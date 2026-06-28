import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
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
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "49"))
CLEANUP_MAX_AGE_HOURS = int(os.getenv("CLEANUP_MAX_AGE_HOURS", "24"))
CHAT_HISTORY_LIMIT = int(os.getenv("CHAT_HISTORY_LIMIT", "12"))
AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", os.getenv("GROQ_TIMEOUT_SECONDS", "45")))

MAX_FILE_SIZE = MAX_DOWNLOAD_MB * 1024 * 1024
DOWNLOAD_DIR = Path("downloads")
SYSTEM_PROMPT_PATH = Path("/app/system_prompt.txt")
INSTAGRAM_COOKIES_PATH = Path("/app/instagram_cookies.txt")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

SUPPORTED_HINTS = ("tiktok.com", "instagram.com", "youtu.be", "youtube.com")
RESTART_BUTTON_TEXT = "Перезапустить"
GEMINI_API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
XAI_API_URL = "https://api.x.ai/v1/chat/completions"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(RESTART_BUTTON_TEXT)]],
    resize_keyboard=True,
    input_field_placeholder="Напиши сообщение или отправь ссылку на видео",
)

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
    return match.group(0).strip()


def looks_supported(url: str) -> bool:
    lowered = url.lower()
    return any(hint in lowered for hint in SUPPORTED_HINTS)


def sanitize_name(value: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', "_", value).strip()
    return safe[:80] or "video"


def build_output_template(job_dir: Path) -> str:
    return str(job_dir / "%(title).80s-%(id)s.%(ext)s")


def build_ydl_opts(skip_download: bool = False, outtmpl: Optional[str] = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        "restrictfilenames": False,
    }

    if skip_download:
        opts["skip_download"] = True

    if outtmpl:
        opts["outtmpl"] = outtmpl
        opts["format"] = "best[ext=mp4]/best"
        opts["noplaylist"] = True

    if INSTAGRAM_COOKIES_PATH.exists():
        opts["cookiefile"] = str(INSTAGRAM_COOKIES_PATH)

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


def resolve_ai_config() -> tuple[str, str, str]:
    if AI_PROVIDER == "gemini":
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not configured.")
        return "gemini", GEMINI_API_URL_TEMPLATE.format(model=GEMINI_MODEL), GEMINI_MODEL

    if AI_PROVIDER == "xai":
        if not XAI_API_KEY:
            raise RuntimeError("XAI_API_KEY is not configured.")
        return "xai", XAI_API_URL, XAI_MODEL

    if AI_PROVIDER == "groq":
        if not GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY is not configured.")
        return "groq", GROQ_API_URL, GROQ_MODEL

    if GEMINI_API_KEY:
        return "gemini", GEMINI_API_URL_TEMPLATE.format(model=GEMINI_MODEL), GEMINI_MODEL

    if XAI_API_KEY:
        return "xai", XAI_API_URL, XAI_MODEL

    if GROQ_API_KEY:
        return "groq", GROQ_API_URL, GROQ_MODEL

    raise RuntimeError("No AI provider key is configured.")


async def safe_edit_status(message, text: str) -> None:
    try:
        await message.edit_text(text)
    except Exception:
        logger.exception("Failed to edit status message")
        await message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def safe_delete_message(message) -> None:
    try:
        await message.delete()
    except Exception:
        logger.exception("Failed to delete message")


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
        if isinstance(candidate, int) and candidate > 0:
            return candidate
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
    provider, api_url, model = resolve_ai_config()
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
                "Authorization": f"Bearer {XAI_API_KEY if provider == 'xai' else GROQ_API_KEY}",
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

    status = await message.reply_text("Проверяю ссылку и доступность видео...")

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
        logger.exception("Download failed")
        details = str(exc)[:500]
        if "Requested content is not available, rate-limit reached or login required" in details:
            text = (
                "Instagram уперся в логин или лимиты. Чтобы такие ссылки снова качались, нужно "
                "подложить cookies от залогиненного Instagram в `/opt/skachat/instagram_cookies.txt`, "
                "потом перезапустить `bash /opt/skachat/deploy.sh`.\n\n"
                f"Детали: {details}"
            )
        else:
            text = (
                "Не удалось скачать видео. Возможно, ссылка приватная, ограничена по региону или временно не поддерживается.\n\n"
                f"Детали: {details}"
            )
        await safe_edit_status(status, text)
    except Exception as exc:
        logger.exception("Unexpected download error")
        await safe_edit_status(
            status,
            "Произошла ошибка во время скачивания. Нажми «Перезапустить», если хочешь сбросить состояние, "
            f"и попробуй еще раз.\n\nДетали: {str(exc)[:300]}"
        )
    finally:
        if "job_dir" in locals() and job_dir.exists():
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
    except Exception as exc:
        logger.exception("Unexpected chat error")
        await message.reply_text(
            "Не получилось ответить сейчас. Нажми «Перезапустить» и попробуй еще раз.\n\n"
            f"Детали: {str(exc)[:200]}",
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

    cleanup_stale_downloads()

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

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    cleanup_stale_downloads()

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset_dialog))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is running")
    application.run_polling()


if __name__ == "__main__":
    main()
