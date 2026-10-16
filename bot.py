import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
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
# ИИ-чат — только DeepSeek: он отвечает с российского хостинга, тогда как
# Gemini и Groq режут запросы по региону. API у него OpenAI-совместимый.
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
# deepseek-flash — базовая модель. Режим размышлений у неё включён по
# умолчанию, для чата он только замедляет ответ и раздувает счёт, поэтому
# выключаем его, если не попросили явно.
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
DEEPSEEK_THINKING = os.getenv("DEEPSEEK_THINKING", "disabled").strip().lower()
AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "1024"))
# Сколько сообщений ИИ один пользователь может отправить за сутки (по Москве).
# 0 — без ограничения. Администраторы из ADMIN_USER_IDS лимита не имеют.
AI_DAILY_LIMIT = int(os.getenv("AI_DAILY_LIMIT", "10"))
ADMIN_USER_IDS = {
    int(item) for item in re.split(r"[,\s]+", os.getenv("ADMIN_USER_IDS", "")) if item.strip().isdigit()
}
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "49"))
# Прокси только для TikTok, например socks5://user:pass@host:1080 — он
# блокирует российские адреса. Пусто — качать напрямую.
TIKTOK_PROXY = os.getenv("TIKTOK_PROXY", "").strip()
CLEANUP_MAX_AGE_HOURS = int(os.getenv("CLEANUP_MAX_AGE_HOURS", "24"))
CHAT_HISTORY_LIMIT = int(os.getenv("CHAT_HISTORY_LIMIT", "10"))
AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", "60"))

# Обязательная подписка. Каналы перечисляются в REQUIRED_CHANNELS через
# запятую; пустое значение выключает проверку. Старая пара REQUIRED_CHANNEL
# и REQUIRED_CHANNEL_URL по-прежнему понимается — разбор см. в parse_channels().
REQUIRED_CHANNELS_RAW = os.getenv("REQUIRED_CHANNELS", "").strip()
if not REQUIRED_CHANNELS_RAW and os.getenv("REQUIRED_CHANNEL", "").strip():
    REQUIRED_CHANNELS_RAW = "|".join(
        part
        for part in (os.getenv("REQUIRED_CHANNEL", "").strip(), os.getenv("REQUIRED_CHANNEL_URL", "").strip())
        if part
    )
SUBSCRIPTION_CACHE_SECONDS = int(os.getenv("SUBSCRIPTION_CACHE_SECONDS", "300"))

# Concurrency / reliability knobs.
CONCURRENT_UPDATES = int(os.getenv("CONCURRENT_UPDATES", "8"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))

MAX_FILE_SIZE = MAX_DOWNLOAD_MB * 1024 * 1024
BASE_DIR = Path(__file__).resolve().parent
# Defaults resolve next to bot.py so the bot runs both locally and in Docker
# (WORKDIR /app), regardless of the current working directory.
DOWNLOAD_DIR = BASE_DIR / "downloads"
# Счётчики лимита держим в файле, иначе перезапуск бота обнулял бы их
AI_USAGE_PATH = Path(os.getenv("AI_USAGE_FILE", str(BASE_DIR / "data" / "ai_usage.json")))
MOSCOW_TZ = timezone(timedelta(hours=3))
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
CHAT_COMPLETIONS_PATH = "/chat/completions"
TELEGRAM_TEXT_LIMIT = 4096

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


# Ответ уходит в Telegram обычным текстом, и звёздочки с решётками Markdown
# видны как есть. Добавляем это правило в код, чтобы не зависеть от того,
# что написано в личном system_prompt.txt.
PLAIN_TEXT_RULE = (
    "Отвечай обычным текстом без Markdown-разметки: не используй **, __, # и `. "
    "Списки оформляй дефисами или цифрами."
)
SYSTEM_PROMPT = f"{load_system_prompt()}\n\n{PLAIN_TEXT_RULE}"


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


def is_tiktok(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    return hostname == "tiktok.com" or hostname.endswith(".tiktok.com")


def build_ydl_opts(url: str, skip_download: bool = False, outtmpl: Optional[str] = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        "restrictfilenames": True,
        "noplaylist": True,
        # Готовый mp4 со звуком, а если его нет (YouTube без JS-движка отдаёт
        # только раздельные потоки) — видео и звук по отдельности со склейкой
        # через ffmpeg. Формат задаём и для пробы, иначе оценка размера
        # считалась бы по другому файлу, чем тот, что потом скачивается.
        "format": "b[ext=mp4]/bv*[ext=mp4]+ba[ext=m4a]/b/bv*+ba",
        "merge_output_format": "mp4",
        # Только IPv4: IPv6 с российского хостинга не отвечает, а curl_cffi
        # разрешает имена сам, мимо подмены из net_patch, и висит до таймаута
        "source_address": "0.0.0.0",
    }

    if skip_download:
        opts["skip_download"] = True

    if outtmpl:
        opts["outtmpl"] = outtmpl

    # TikTok не отдаёт ролики на российские адреса, куки тут не помогают —
    # нужен выход в интернет из другой страны
    if TIKTOK_PROXY and is_tiktok(url):
        opts["proxy"] = TIKTOK_PROXY

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


def split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Режем длинный ответ на сообщения по границам абзацев, а не посреди слова."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


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


def build_chat_url(base_url: str) -> str:
    """Адрес дают то как `https://host` или `https://host/v1`, то целиком до метода."""
    url = base_url.rstrip("/")
    if url.endswith(CHAT_COMPLETIONS_PATH):
        return url
    return url + CHAT_COMPLETIONS_PATH


def resolve_ai_config() -> AIConfig:
    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY.startswith("your_"):
        raise AIConfigError("DEEPSEEK_API_KEY is not configured.")
    return AIConfig("deepseek", build_chat_url(DEEPSEEK_BASE_URL), DEEPSEEK_MODEL, DEEPSEEK_API_KEY)


def today_key() -> str:
    return datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d")


class AIUsage:
    """Суточные счётчики сообщений ИИ по пользователям, с сохранением в файл.

    Обработчики работают в одном цикле asyncio, а между проверкой и
    списанием нет await, поэтому гонок между пользователями не бывает.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.day = today_key()
        self.counts: dict[str, int] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("day") == self.day:
                self.counts = {str(k): int(v) for k, v in data.get("counts", {}).items()}
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("Счётчики лимита ИИ не прочитались, начинаем с нуля")

    def _roll_day(self) -> None:
        day = today_key()
        if day != self.day:
            self.day = day
            self.counts = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"day": self.day, "counts": self.counts}), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            logger.exception("Не удалось сохранить счётчики лимита ИИ в %s", self.path)

    def is_limited(self, user_id: int) -> bool:
        return AI_DAILY_LIMIT > 0 and user_id not in ADMIN_USER_IDS

    def remaining(self, user_id: int) -> int:
        self._roll_day()
        return max(0, AI_DAILY_LIMIT - self.counts.get(str(user_id), 0))

    def take(self, user_id: int) -> bool:
        """Списать одно сообщение. False — лимит на сегодня исчерпан."""
        if not self.is_limited(user_id):
            return True
        if self.remaining(user_id) <= 0:
            return False
        key = str(user_id)
        self.counts[key] = self.counts.get(key, 0) + 1
        self._save()
        return True

    def refund(self, user_id: int) -> None:
        """Вернуть сообщение, если ответа не случилось по вине бота или ИИ."""
        if not self.is_limited(user_id):
            return
        self._roll_day()
        key = str(user_id)
        if self.counts.get(key, 0) > 0:
            self.counts[key] -= 1
            self._save()


ai_usage = AIUsage(AI_USAGE_PATH)


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


class Channel(NamedTuple):
    chat_id: str  # @username или числовой id вида -100…
    url: str      # куда ведёт кнопка «Подписаться»
    label: str    # подпись на кнопке


def parse_channels(raw: str) -> list[Channel]:
    """Разобрать REQUIRED_CHANNELS.

    Каналы перечисляются через запятую, точку с запятой или с новой строки.
    Каждый можно записать так:
      https://t.me/name  или  t.me/name  или  @name   — публичный канал;
      -1001234567890|https://t.me/+ссылка             — закрытый канал:
                                                         id и пригласительная ссылка.
    """
    channels: list[Channel] = []
    seen: set[str] = set()
    for item in re.split(r"[,;\n]+", raw):
        item = item.strip()
        if not item:
            continue
        chat_part, _, url = (part.strip() for part in item.partition("|"))
        match = re.fullmatch(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})/?", chat_part)
        if match:
            chat_id = "@" + match.group(1)
        elif re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,}", chat_part):
            chat_id = "@" + chat_part.lstrip("@")
        elif re.fullmatch(r"-100\d+", chat_part):
            chat_id = chat_part
        else:
            logger.error("REQUIRED_CHANNELS: не понял запись %r — канал пропущен", item)
            continue
        if chat_id.lower() in seen:
            continue
        seen.add(chat_id.lower())
        if not url and chat_id.startswith("@"):
            url = f"https://t.me/{chat_id[1:]}"
        if not url:
            logger.error("REQUIRED_CHANNELS: у закрытого канала %s нет ссылки после «|»", chat_id)
        label = chat_id if chat_id.startswith("@") else f"канал {len(channels) + 1}"
        channels.append(Channel(chat_id, url, label))
    return channels


REQUIRED_CHANNELS = parse_channels(REQUIRED_CHANNELS_RAW)


def build_subscription_keyboard(channels: list[Channel]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(f"📢 Подписаться: {channel.label}", url=channel.url)]
        for channel in channels
        if channel.url
    ]
    buttons.append([InlineKeyboardButton("✅ Я подписался", callback_data=VERIFY_SUBSCRIPTION_CALLBACK)])
    return InlineKeyboardMarkup(buttons)


async def is_channel_member(context: ContextTypes.DEFAULT_TYPE, channel: Channel, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=channel.chat_id, user_id=user_id)
    except BadRequest as exc:
        # Telegram не даёт смотреть подписчиков: бот не админ канала или имя
        # канала неверное. Закрыть бота для всех из-за ошибки настройки хуже,
        # чем временно пропустить без проверки, — поэтому пускаем и шумим в журнал.
        if "user not found" in str(exc).lower():
            return False
        logger.error(
            "Не удалось проверить подписку на %s (%s) — пропускаем без проверки. "
            "Сделайте бота администратором канала.",
            channel.chat_id,
            exc,
        )
        return True
    except Exception:
        # Сетевой сбой: не отказываем подписчику из-за моргнувшей связи
        logger.exception("Subscription check failed for %s, user_id=%s", channel.chat_id, user_id)
        return True
    if member.status in ("member", "administrator", "creator"):
        return True
    # Подписчик с ограничениями в канале — всё равно подписчик
    return member.status == "restricted" and bool(getattr(member, "is_member", False))


async def missing_channels(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> list[Channel]:
    """Каналы, на которые пользователь не подписан. Проверяем все разом, а не по очереди."""
    results = await asyncio.gather(
        *(is_channel_member(context, channel, user_id) for channel in REQUIRED_CHANNELS)
    )
    missing = [channel for channel, ok in zip(REQUIRED_CHANNELS, results) if not ok]
    context.user_data["sub_verified"] = not missing
    context.user_data["sub_verified_until"] = time.time() + SUBSCRIPTION_CACHE_SECONDS
    return missing


def subscription_prompt(missing: list[Channel]) -> str:
    if len(REQUIRED_CHANNELS) == 1:
        return "Чтобы пользоваться ботом, подпишись на канал и нажми «Я подписался»."
    names = ", ".join(channel.label for channel in missing)
    return f"Чтобы пользоваться ботом, подпишись на каналы ({names}) и нажми «Я подписался»."


async def ensure_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not REQUIRED_CHANNELS:
        return True

    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return False

    if user.id in ADMIN_USER_IDS:
        return True

    if context.user_data.get("sub_verified") and time.time() < context.user_data.get("sub_verified_until", 0):
        return True

    missing = await missing_channels(context, user.id)
    if missing:
        # Кнопки только для тех каналов, где подписки ещё нет
        await message.reply_text(
            subscription_prompt(missing),
            reply_markup=build_subscription_keyboard(missing),
        )
    return not missing


async def handle_subscription_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return

    missing = await missing_channels(context, query.from_user.id)

    if not missing:
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
        return

    names = ", ".join(channel.label for channel in missing)
    await query.answer(f"Нет подписки: {names}. Подпишись и попробуй еще раз.", show_alert=True)
    try:
        # Обновляем кнопки: оставляем только каналы, где подписки всё ещё нет
        await query.edit_message_text(
            subscription_prompt(missing), reply_markup=build_subscription_keyboard(missing)
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            logger.exception("Failed to update subscription prompt message")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error

    # Сеть до Telegram с российского хостинга изредка моргает; PTB сам
    # повторяет запрос, поэтому traceback на каждое моргание — лишний шум.
    if isinstance(error, NetworkError):
        logger.warning("Сеть до Telegram моргнула: %s", error)
        return

    logger.error("Необработанная ошибка", exc_info=error)


async def post_init(application: Application) -> None:
    for channel in REQUIRED_CHANNELS:
        try:
            me = await application.bot.get_chat_member(channel.chat_id, application.bot.id)
        except Exception as exc:
            logger.error(
                "Канал %s: не удалось проверить права бота (%s). Проверьте запись в "
                "REQUIRED_CHANNELS и что бот добавлен в канал администратором — до тех пор "
                "подписка на этот канал не проверяется.",
                channel.chat_id,
                exc,
            )
            continue
        if me.status in ("administrator", "creator"):
            logger.info("Subscription gate active for channel %s", channel.chat_id)
        else:
            logger.error(
                "Бот не администратор канала %s — подписка на него не проверяется. "
                "Добавьте бота в администраторы канала.",
                channel.chat_id,
            )


def probe_video(url: str) -> dict:
    opts = build_ydl_opts(url, skip_download=True)
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _size_of(item: dict) -> Optional[int]:
    for candidate in (item.get("filesize"), item.get("filesize_approx")):
        # yt-dlp может вернуть размер как float (filesize_approx), учитываем оба типа
        if isinstance(candidate, (int, float)) and candidate > 0:
            return int(candidate)
    return None


def choose_estimated_size(info: dict) -> Optional[int]:
    size = _size_of(info)
    if size:
        return size

    # Видео и звук качаются по отдельности: итоговый файл — их сумма,
    # а не размер одной лишь видеодорожки
    requested = info.get("requested_formats") or []
    sizes = [_size_of(item) for item in requested]
    if sizes and all(sizes):
        return sum(sizes)
    return None


def download_video(url: str, job_dir: Path) -> Path:
    job_dir.mkdir(parents=True, exist_ok=True)
    opts = build_ydl_opts(url, outtmpl=build_output_template(job_dir))

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

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": user_text}],
        "max_tokens": AI_MAX_TOKENS,
        "thinking": {"type": "enabled" if DEEPSEEK_THINKING == "enabled" else "disabled"},
    }
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(api_url, headers=headers, json=payload)
        response.raise_for_status()

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"{provider} returned no choices: {str(data)[:300]}")
    # reasoning_content (ход размышлений) пользователю не показываем — только ответ
    content = choices[0].get("message", {}).get("content") or ""
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
        "Если пришлешь обычный текст, я просто отвечу как собеседник"
        + (f" (до {AI_DAILY_LIMIT} сообщений в сутки)" if AI_DAILY_LIMIT > 0 else "")
        + ".\n"
        f"Кнопка «{RESTART_BUTTON_TEXT}» сбрасывает только твой диалог.",
        reply_markup=MAIN_KEYBOARD,
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подсказать свой Telegram ID — он нужен для ADMIN_USER_IDS. Без проверки подписки."""
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    text = f"Твой Telegram ID: {user_id}"
    if user_id in ADMIN_USER_IDS:
        text += "\nТы в списке администраторов: лимит ИИ на тебя не действует."
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


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
                if "IP address is blocked" in details:
                    # TikTok не показывает ролики российским адресам. Лечится
                    # только прокси из другой страны (TIKTOK_PROXY), не куками.
                    logger.warning(
                        "TikTok blocks the host IP for url=%s. Set TIKTOK_PROXY to a proxy outside Russia.",
                        url,
                    )
                    text = (
                        "TikTok не отдаёт это видео серверу бота — площадка блокирует его адрес. "
                        "Ссылка тут ни при чём. Администратор бота уже знает, попробуй позже "
                        "или пришли ссылку на YouTube или Instagram."
                    )
                elif "Unexpected response from webpage request" in details:
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

    user_id = update.effective_user.id if update.effective_user else message.chat_id
    if not ai_usage.take(user_id):
        await message.reply_text(
            f"На сегодня лимит общения с ИИ исчерпан: {AI_DAILY_LIMIT} сообщений в сутки. "
            "Счётчик обнулится в полночь по Москве. Скачивание видео работает без ограничений.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    history = context.user_data.get("chat_history", [])
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    try:
        answer = await ask_ai(text, history)
    except httpx.HTTPStatusError as exc:
        ai_usage.refund(user_id)
        status_code = exc.response.status_code
        logger.error("DeepSeek вернул %s: %s", status_code, exc.response.text[:500])
        if status_code == 401:
            error_text = "Чат временно недоступен: ключ ИИ недействителен. Администратор бота уже знает."
        elif status_code == 402:
            # Insufficient Balance — на счёте DeepSeek кончились деньги
            logger.error("На балансе DeepSeek закончились средства — пополните счёт на platform.deepseek.com")
            error_text = "Чат временно недоступен: у бота закончился баланс ИИ. Скачивание видео работает."
        elif status_code == 429:
            error_text = "ИИ сейчас перегружен. Попробуй ещё раз через минуту."
        elif status_code >= 500:
            error_text = "У ИИ-сервиса временный сбой. Попробуй ещё раз чуть позже."
        else:
            error_text = f"ИИ вернул ошибку {status_code}. Нажми «Перезапустить» и попробуй ещё раз."

        await message.reply_text(error_text, reply_markup=MAIN_KEYBOARD)
        return
    except httpx.TimeoutException:
        ai_usage.refund(user_id)
        logger.warning("Запрос к DeepSeek не уложился в %s с", AI_TIMEOUT_SECONDS)
        await message.reply_text(
            "ИИ отвечает слишком долго. Попробуй ещё раз через минуту.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    except httpx.TransportError as exc:
        ai_usage.refund(user_id)
        logger.warning("Нет связи с DeepSeek: %r", exc)
        await message.reply_text(
            "Не получилось связаться с ИИ. Попробуй ещё раз через минуту.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    except AIConfigError as exc:
        ai_usage.refund(user_id)
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
        ai_usage.refund(user_id)
        logger.exception("Unexpected chat error")
        await message.reply_text(
            "Не получилось ответить сейчас. Нажми «Перезапустить» и попробуй еще раз.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if not answer:
        ai_usage.refund(user_id)
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

    if ai_usage.is_limited(user_id):
        left = ai_usage.remaining(user_id)
        if left <= 2:
            answer += f"\n\n(Осталось сообщений ИИ на сегодня: {left})"

    for chunk in split_text(answer):
        await message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)


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

    if REQUIRED_CHANNELS:
        logger.info("Required channels: %s", ", ".join(channel.chat_id for channel in REQUIRED_CHANNELS))

    try:
        ai_config = resolve_ai_config()
    except AIConfigError as exc:
        logger.warning(
            "Chat is disabled: %s Downloads will still work. "
            "Put DEEPSEEK_API_KEY into .env and restart.",
            exc,
        )
    else:
        logger.info(
            "AI provider: %s, model %s, thinking %s, daily limit %s per user (admins: %s)",
            ai_config.provider,
            ai_config.model,
            DEEPSEEK_THINKING,
            AI_DAILY_LIMIT or "off",
            len(ADMIN_USER_IDS),
        )

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
    application.add_handler(CommandHandler("id", show_id))
    application.add_handler(
        CallbackQueryHandler(handle_subscription_check, pattern=f"^{VERIFY_SUBSCRIPTION_CALLBACK}$")
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(on_error)

    logger.info("Bot is running")
    application.run_polling()


if __name__ == "__main__":
    main()
