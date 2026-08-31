import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import NamedTuple, Optional
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
AI_PROVIDER = os.getenv("AI_PROVIDER", "auto").strip().lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", "")).strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
XAI_API_KEY = os.getenv("XAI_API_KEY", "").strip()
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4.3").strip()
# Любой шлюз с OpenAI-совместимым API: агрегаторы моделей, self-hosted прокси
# и т. п. Задаётся тремя переменными, потому что адрес заранее неизвестен.
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "").strip()
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "49"))
CLEANUP_MAX_AGE_HOURS = int(os.getenv("CLEANUP_MAX_AGE_HOURS", "24"))
CHAT_HISTORY_LIMIT = int(os.getenv("CHAT_HISTORY_LIMIT", "12"))
AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", os.getenv("GROQ_TIMEOUT_SECONDS", "45")))

# Channel-subscription gate. Leave REQUIRED_CHANNEL empty to disable the gate entirely.
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "").strip()
SUBSCRIPTION_CACHE_SECONDS = int(os.getenv("SUBSCRIPTION_CACHE_SECONDS", "300"))

# Concurrency / reliability knobs.
CONCURRENT_UPDATES = int(os.getenv("CONCURRENT_UPDATES", "8"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))

MAX_FILE_SIZE = MAX_DOWNLOAD_MB * 1024 * 1024
BASE_DIR = Path(__file__).resolve().parent
# Defaults resolve next to bot.py so the bot runs both locally and in Docker
# (WORKDIR /app), regardless of the current working directory.
DOWNLOAD_DIR = BASE_DIR / "downloads"
SYSTEM_PROMPT_PATH = Path(os.getenv("SYSTEM_PROMPT_PATH", str(BASE_DIR / "system_prompt.txt")))
# Netscape-формат хранит куки любых доменов в одном файле, поэтому один
# cookies.txt закрывает и Instagram, и TikTok. Старое имя оставлено, чтобы
# у тех, кто уже положил instagram_cookies.txt, ничего не сломалось.
COOKIES_PATH = Path(os.getenv("COOKIES_FILE", str(BASE_DIR / "cookies.txt")))
INSTAGRAM_COOKIES_PATH = Path(os.getenv("INSTAGRAM_COOKIES_PATH", str(BASE_DIR / "instagram_cookies.txt")))
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

ALLOWED_DOMAINS = (
    "tiktok.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
)
RESTART_BUTTON_TEXT = "Перезапустить"
VERIFY_SUBSCRIPTION_CALLBACK = "verify_subscription"
GEMINI_API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
XAI_API_URL = "https://api.x.ai/v1/chat/completions"
CHAT_COMPLETIONS_PATH = "/chat/completions"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(RESTART_BUTTON_TEXT)]],
    resize_keyboard=True,
    input_field_placeholder="Напиши сообщение или отправь ссылку на видео",
)

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_system_prompt() -> str:
    try:
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except Exception as exc:
        raise RuntimeError(f"Failed to load system prompt from {SYSTEM_PROMPT_PATH}") from exc


SYSTEM_PROMPT = load_system_prompt()


def extract_url(text: str) -> Optional[str]:
    match = URL_RE.search(text or "")
    if not match:
        return None
    url = match.group(0).strip()
    # Отрезаем хвостовую пунктуацию: "смотри (https://youtu.be/x)," -> корректная ссылка
    url = url.rstrip(".,;:!?\"'»›")
    if url.endswith(")") and "(" not in url:
        url = url.rstrip(")")
    return url or None


def looks_supported(url: str) -> bool:
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False

    if not hostname:
        return False

    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in ALLOWED_DOMAINS)


def sanitize_name(value: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', "_", value).strip()
    return safe[:80] or "video"


def build_output_template(job_dir: Path) -> str:
    return str(job_dir / "%(title).80s-%(id)s.%(ext)s")


def resolve_cookiefile() -> Optional[Path]:
    for path in (COOKIES_PATH, INSTAGRAM_COOKIES_PATH):
        if path.exists():
            return path
    return None


def build_ydl_opts(skip_download: bool = False, outtmpl: Optional[str] = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        "restrictfilenames": True,
    }

    if skip_download:
        opts["skip_download"] = True

    if outtmpl:
        opts["outtmpl"] = outtmpl
        opts["format"] = "best[ext=mp4]/best"
        opts["noplaylist"] = True

    cookiefile = resolve_cookiefile()
    if cookiefile is not None:
        opts["cookiefile"] = str(cookiefile)

    return opts


def cleanup_stale_downloads() -> None:
    if not DOWNLOAD_DIR.exists():
        return

    cutoff = time.time() - (CLEANUP_MAX_AGE_HOURS * 3600)
    for path in DOWNLOAD_DIR.iterdir():
        try:
            if path.stat().st_mtime >= cutoff:
                continue

            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except Exception:
            logger.exception("Failed to clean up %s", path)


def reset_user_state(user_data: dict) -> None:
    user_data.pop("chat_history", None)


def trim_history(history: list[dict]) -> list[dict]:
    if CHAT_HISTORY_LIMIT <= 0:
        return []
    return history[-CHAT_HISTORY_LIMIT:]


class AIConfigError(RuntimeError):
    """Провайдер ИИ не настроен. Лечится правкой .env, а не перезапуском."""


class AIConfig(NamedTuple):
    provider: str
    api_url: str
    model: str
    api_key: str


def build_openai_url(base_url: str) -> str:
    """Шлюзы дают адрес то как `https://host/v1`, то целиком до метода."""
    url = base_url.rstrip("/")
    if url.endswith(CHAT_COMPLETIONS_PATH):
        return url
    return url + CHAT_COMPLETIONS_PATH


def gemini_config() -> AIConfig:
    return AIConfig(
        "gemini",
        GEMINI_API_URL_TEMPLATE.format(model=GEMINI_MODEL),
        GEMINI_MODEL,
        GEMINI_API_KEY,
    )


def openai_compatible_config() -> AIConfig:
    if not OPENAI_MODEL:
        raise AIConfigError(
            "OPENAI_MODEL is not configured. A gateway serves many models, "
            "so the model name cannot be guessed."
        )
    return AIConfig(
        "openai_compatible",
        build_openai_url(OPENAI_BASE_URL),
        OPENAI_MODEL,
        OPENAI_API_KEY,
    )


def resolve_ai_config() -> AIConfig:
    if AI_PROVIDER in ("openai", "openai_compatible"):
        if not (OPENAI_BASE_URL and OPENAI_API_KEY):
            raise AIConfigError("OPENAI_BASE_URL and OPENAI_API_KEY are not configured.")
        return openai_compatible_config()

    if AI_PROVIDER == "gemini":
        if not GEMINI_API_KEY:
            raise AIConfigError("GEMINI_API_KEY is not configured.")
        return gemini_config()

    if AI_PROVIDER == "xai":
        if not XAI_API_KEY:
            raise AIConfigError("XAI_API_KEY is not configured.")
        return AIConfig("xai", XAI_API_URL, XAI_MODEL, XAI_API_KEY)

    if AI_PROVIDER == "groq":
        if not GROQ_API_KEY:
            raise AIConfigError("GROQ_API_KEY is not configured.")
        return AIConfig("groq", GROQ_API_URL, GROQ_MODEL, GROQ_API_KEY)

    # Свой шлюз идёт первым: его адрес прописывают осознанно, а ключи
    # остальных провайдеров нередко остаются в .env с прошлых настроек.
    if OPENAI_BASE_URL and OPENAI_API_KEY:
        return openai_compatible_config()

    if GEMINI_API_KEY:
        return gemini_config()

    if XAI_API_KEY:
        return AIConfig("xai", XAI_API_URL, XAI_MODEL, XAI_API_KEY)

    if GROQ_API_KEY:
        return AIConfig("groq", GROQ_API_URL, GROQ_MODEL, GROQ_API_KEY)

    raise AIConfigError("No AI provider key is configured.")


async def safe_edit_status(message, text: str) -> None:
    try:
        await message.edit_text(text)
    except BadRequest as exc:
        # "Message is not modified" — текст совпал со старым, дублировать не нужно
        if "not modified" in str(exc).lower():
            return
        logger.exception("Failed to edit status message")
        await message.reply_text(text, reply_markup=MAIN_KEYBOARD)
    except Exception:
        logger.exception("Failed to edit status message")
        await message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def safe_delete_message(message) -> None:
    try:
        await message.delete()
    except Exception:
        logger.exception("Failed to delete message")


def build_subscription_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    if REQUIRED_CHANNEL_URL:
        buttons.append([InlineKeyboardButton("📢 Открыть канал", url=REQUIRED_CHANNEL_URL)])
    buttons.append([InlineKeyboardButton("✅ Я подписался", callback_data=VERIFY_SUBSCRIPTION_CALLBACK)])
    return InlineKeyboardMarkup(buttons)


async def is_channel_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
    except Exception:
        logger.exception("Subscription check failed for user_id=%s", user_id)
        return False
    return member.status in ("member", "administrator", "creator")


async def ensure_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not REQUIRED_CHANNEL:
        return True

    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return False

    if context.user_data.get("sub_verified") and time.time() < context.user_data.get("sub_verified_until", 0):
        return True

    subscribed = await is_channel_member(context, user.id)
    context.user_data["sub_verified"] = subscribed
    context.user_data["sub_verified_until"] = time.time() + SUBSCRIPTION_CACHE_SECONDS

    if not subscribed:
        await message.reply_text(
            "Чтобы пользоваться ботом, подпишись на канал и нажми «Я подписался».",
            reply_markup=build_subscription_keyboard(),
        )
    return subscribed


async def handle_subscription_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return

    subscribed = await is_channel_member(context, query.from_user.id)
    context.user_data["sub_verified"] = subscribed
    context.user_data["sub_verified_until"] = time.time() + SUBSCRIPTION_CACHE_SECONDS

    if subscribed:
        await query.answer("Подписка подтверждена!")
        try:
            await query.edit_message_text("Спасибо за подписку! Бот доступен.")
        except Exception:
            logger.exception("Failed to edit subscription prompt message")
        if query.message:
            await query.message.reply_text(
                "Отправь ссылку на TikTok, Instagram или YouTube, и я попробую скачать видео.\n\n"
                "Если пришлешь обычный текст, я просто отвечу как собеседник.",
                reply_markup=MAIN_KEYBOARD,
            )
    else:
        await query.answer(
            "Подписка не найдена. Подпишись на канал и попробуй еще раз.",
            show_alert=True,
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error

    # Сеть до Telegram с российского хостинга изредка моргает; PTB сам
    # повторяет запрос, поэтому traceback на каждое моргание — лишний шум.
    if isinstance(error, NetworkError):
        logger.warning("Сеть до Telegram моргнула: %s", error)
        return

    logger.error("Необработанная ошибка", exc_info=error)


async def post_init(application: Application) -> None:
    if not REQUIRED_CHANNEL:
        return

    try:
        me = await application.bot.get_chat_member(REQUIRED_CHANNEL, application.bot.id)
        if me.status not in ("administrator", "creator"):
            logger.warning(
                "Bot is not an administrator of %s — subscription checks will always fail. "
                "Add the bot as an administrator of the channel.",
                REQUIRED_CHANNEL,
            )
        else:
            logger.info("Subscription gate active for channel %s", REQUIRED_CHANNEL)
    except Exception:
        logger.exception(
            "Could not verify bot membership in %s. Make sure REQUIRED_CHANNEL is correct "
            "and the bot is added as an administrator of the channel.",
            REQUIRED_CHANNEL,
        )


def probe_video(url: str) -> dict:
    opts = build_ydl_opts(skip_download=True)
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def choose_estimated_size(info: dict) -> Optional[int]:
    candidates = [
        info.get("filesize"),
        info.get("filesize_approx"),
    ]

    requested = info.get("requested_formats") or []
    for item in requested:
        candidates.append(item.get("filesize"))
        candidates.append(item.get("filesize_approx"))

    for candidate in candidates:
        # yt-dlp может вернуть размер как float (filesize_approx), учитываем оба типа
        if isinstance(candidate, (int, float)) and candidate > 0:
            return int(candidate)
    return None


def download_video(url: str, job_dir: Path) -> Path:
    job_dir.mkdir(parents=True, exist_ok=True)
    opts = build_ydl_opts(outtmpl=build_output_template(job_dir))

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        downloaded_path = Path(ydl.prepare_filename(info))

    if downloaded_path.exists():
        return downloaded_path

    mp4_candidate = downloaded_path.with_suffix(".mp4")
    if mp4_candidate.exists():
        return mp4_candidate

    files = sorted(job_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise FileNotFoundError("Downloaded file was not found.")
    return files[0]


async def ask_ai(user_text: str, history: list[dict]) -> str:
    config = resolve_ai_config()
    provider, api_url, model = config.provider, config.api_url, config.model
    timeout = httpx.Timeout(AI_TIMEOUT_SECONDS)

    async with httpx.AsyncClient(timeout=timeout) as client:
        if provider == "gemini":
            contents = []
            for item in history:
                role = "model" if item["role"] == "assistant" else "user"
                contents.append({"role": role, "parts": [{"text": item["content"]}]})
            contents.append({"role": "user", "parts": [{"text": user_text}]})

            payload = {
                "systemInstruction": {
                    "parts": [{"text": SYSTEM_PROMPT}],
                },
                "contents": contents,
            }
            headers = {
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json",
            }
        else:
            payload = {
                "model": model,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": user_text}],
            }
            headers = {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            }

        response = await client.post(api_url, headers=headers, json=payload)
        response.raise_for_status()

    data = response.json()
    if provider == "gemini":
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"Gemini returned no candidates: {data}")
        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = [part.get("text", "") for part in parts if isinstance(part, dict)]
        return "".join(text_parts).strip()

    content = data["choices"][0]["message"]["content"]
    if isinstance(content, list):
        parts = [item.get("text", "") for item in content if isinstance(item, dict)]
        content = "".join(parts)
    return str(content).strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    if not await ensure_subscribed(update, context):
        return

    reset_user_state(context.user_data)
    await update.message.reply_text(
        "Отправь ссылку на TikTok, Instagram или YouTube, и я попробую скачать видео.\n\n"
        "Если пришлешь обычный текст, я просто отвечу как собеседник.\n"
        f"Кнопка «{RESTART_BUTTON_TEXT}» сбрасывает только твой диалог.",
        reply_markup=MAIN_KEYBOARD,
    )


async def reset_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    if not await ensure_subscribed(update, context):
        return

    reset_user_state(context.user_data)
    await update.message.reply_text(
        "Готово. Я сбросил только твою историю диалога, можно продолжать с чистого листа.",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
) -> None:
    message = update.message
    if not message:
        return

    if context.user_data.get("downloading"):
        await message.reply_text(
            "У тебя уже идет загрузка, подожди ее завершения.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    context.user_data["downloading"] = True
    job_dir: Optional[Path] = None
    try:
        if download_semaphore.locked():
            status = await message.reply_text(
                "Сервер сейчас занят другими загрузками, подождите немного..."
            )
        else:
            status = await message.reply_text("Проверяю ссылку и доступность видео...")

        async with download_semaphore:
            try:
                info = await asyncio.to_thread(probe_video, url)
                estimated_size = choose_estimated_size(info)
                title = sanitize_name(info.get("title") or "video")

                if estimated_size and estimated_size > MAX_FILE_SIZE:
                    await safe_edit_status(
                        status,
                        f"Видео слишком большое для отправки ботом: примерно {estimated_size / 1024 / 1024:.1f} MB.\n"
                        f"Текущий лимит: {MAX_DOWNLOAD_MB} MB."
                    )
                    return

                await safe_edit_status(status, f"Скачиваю: {title}")
                await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)

                job_dir = DOWNLOAD_DIR / uuid.uuid4().hex
                file_path = await asyncio.to_thread(download_video, url, job_dir)

                actual_size = file_path.stat().st_size
                if actual_size > MAX_FILE_SIZE:
                    await safe_edit_status(
                        status,
                        f"Файл скачался, но оказался слишком большим: {actual_size / 1024 / 1024:.1f} MB.\n"
                        f"Текущий лимит: {MAX_DOWNLOAD_MB} MB."
                    )
                    return

                caption = f"{title}\nИсточник: {url}"
                with file_path.open("rb") as file_handle:
                    await message.reply_document(
                        document=file_handle,
                        caption=caption[:1024],
                        reply_markup=MAIN_KEYBOARD,
                    )

                await safe_delete_message(status)
            except DownloadError as exc:
                logger.exception("Download failed for url=%s", url)
                details = str(exc)
                if "Unexpected response from webpage request" in details:
                    # Площадка отдала 200, но вместо страницы видео — заслон
                    # антибота (у TikTok это SlardarWAF, признак x-tt-system-error).
                    # Версия yt-dlp тут ни при чём, лечится файлом cookies.
                    logger.warning(
                        "Anti-bot page instead of the video page for url=%s. The host IP is "
                        "blocked; add browser cookies (cookies.txt next to bot.py or COOKIES_FILE).",
                        url,
                    )
                    text = (
                        "Площадка не отдала это видео серверу бота — сработала защита от автоматических "
                        "запросов. Ссылка тут ни при чём, с другого устройства она откроется. "
                        "Администратор бота уже знает, попробуй позже или пришли ссылку с другой площадки."
                    )
                elif "Requested content is not available, rate-limit reached or login required" in details:
                    logger.warning(
                        "Instagram requires login/cookies. Add instagram_cookies.txt and redeploy to fix."
                    )
                    text = (
                        "Instagram сейчас ограничивает доступ без логина. "
                        "Администратор бота уже знает об этом, попробуй другую ссылку или повтори позже."
                    )
                else:
                    text = (
                        "Не удалось скачать видео. Возможно, ссылка приватная, ограничена по региону "
                        "или временно не поддерживается. Попробуй другую ссылку."
                    )
                await safe_edit_status(status, text)
            except Exception:
                logger.exception("Unexpected download error for url=%s", url)
                await safe_edit_status(
                    status,
                    "Произошла ошибка во время скачивания. Нажми «Перезапустить» и попробуй еще раз.",
                )
    finally:
        context.user_data["downloading"] = False
        if job_dir is not None and job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)


async def handle_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    message = update.message
    if not message:
        return

    history = context.user_data.get("chat_history", [])
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    try:
        answer = await ask_ai(text, history)
    except httpx.HTTPStatusError as exc:
        logger.exception("AI provider returned an HTTP error")
        status_code = exc.response.status_code
        response_text = exc.response.text[:500]
        if status_code == 401:
            error_text = "Не получилось обратиться к AI: похоже, ключ провайдера недействителен или отозван."
        elif status_code == 400 and "User location is not supported" in response_text:
            error_text = (
                "Чат через Gemini на этом VPS сейчас не работает: Google режет API по региону сервера. "
                "Скачивание видео при этом должно работать. Чтобы вернуть чат, нужен другой AI-провайдер "
                "или VPS в поддерживаемой стране."
            )
        elif status_code == 429:
            error_text = "Сервис AI сейчас уперся в лимиты. Попробуй еще раз чуть позже."
        else:
            error_text = f"Сервис AI вернул ошибку {status_code}. Нажми «Перезапустить» и попробуй еще раз."

        await message.reply_text(error_text, reply_markup=MAIN_KEYBOARD)
        return
    except httpx.TimeoutException:
        logger.exception("AI request timed out")
        await message.reply_text(
            "AI отвечает слишком долго. Нажми «Перезапустить» и попробуй еще раз через минуту.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    except AIConfigError as exc:
        # Не пишем traceback: это не сбой, а незаполненный .env, и совет
        # «нажми Перезапустить» тут только сбивает с толку — не поможет.
        logger.error("Чат недоступен, провайдер ИИ не настроен: %s", exc)
        await message.reply_text(
            "Чат пока не работает: у бота не настроен ИИ-провайдер. "
            "Скачивание видео при этом работает — пришли ссылку на TikTok, Instagram или YouTube.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    except Exception:
        logger.exception("Unexpected chat error")
        await message.reply_text(
            "Не получилось ответить сейчас. Нажми «Перезапустить» и попробуй еще раз.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if not answer:
        await message.reply_text(
            "ИИ вернул пустой ответ. Попробуй переформулировать вопрос.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    new_history = [
        *history,
        {"role": "user", "content": text},
        {"role": "assistant", "content": answer},
    ]
    context.user_data["chat_history"] = trim_history(new_history)
    await message.reply_text(answer[:4096], reply_markup=MAIN_KEYBOARD)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    if not await ensure_subscribed(update, context):
        return

    # Уборка в отдельном потоке, чтобы не блокировать обработку сообщений
    await asyncio.to_thread(cleanup_stale_downloads)

    text = message.text.strip()
    if not text:
        return

    if text == RESTART_BUTTON_TEXT:
        await reset_dialog(update, context)
        return

    url = extract_url(text)
    if url and looks_supported(url):
        await handle_download(update, context, url)
        return

    if url:
        await message.reply_text(
            "Я пока умею скачивать ссылки только с TikTok, Instagram и YouTube. "
            "Если хочешь, можешь просто написать мне вопрос текстом.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    await handle_chat(update, context, text)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and configure it.")

    if REQUIRED_CHANNEL and not REQUIRED_CHANNEL_URL:
        logger.warning(
            "REQUIRED_CHANNEL is set but REQUIRED_CHANNEL_URL is empty — the subscribe button will have no link."
        )

    try:
        ai_config = resolve_ai_config()
    except AIConfigError as exc:
        logger.warning(
            "Chat is disabled: %s Downloads will still work. "
            "Fill in one AI provider in .env and restart.",
            exc,
        )
    else:
        logger.info("AI provider: %s, model %s", ai_config.provider, ai_config.model)

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    cleanup_stale_downloads()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(CONCURRENT_UPDATES)
        .post_init(post_init)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset_dialog))
    application.add_handler(
        CallbackQueryHandler(handle_subscription_check, pattern=f"^{VERIFY_SUBSCRIPTION_CALLBACK}$")
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(on_error)

    logger.info("Bot is running")
    application.run_polling()


if __name__ == "__main__":
    main()
