"""
ربات حرفه‌ای دانلود و شناسایی موسیقی برای Instagram و Facebook.

هر لینک معتبر را دانلود می‌کند، ویدیو/عکس را می‌فرستد، صدای ویدیو را به MP3
تبدیل می‌کند و با Shazam نام آهنگ و خواننده را تشخیص می‌دهد.
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import time

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import requests
import yt_dlp

try:
    from shazamio import Shazam  # type: ignore

    _SHAZAM_AVAILABLE = True
except Exception:  # noqa: BLE001
    _SHAZAM_AVAILABLE = False

# ---------------------------------------------------------------------------
# پیکربندی پایه (از متغیر محیطی)
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("media-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = (
    os.environ.get("WEBHOOK_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or ""
).rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
MAX_TELEGRAM_BYTES = 100 * 1024 * 1024
HEIGHT_CAPS = [720]  # یک کیفیت معمولی؛ از درخواست‌های چندکیفی جلوگیری می‌کند
COOKIES_FILE = os.environ.get("COOKIES_FILE")
INSTAGRAM_COOKIES_B64 = os.environ.get("INSTAGRAM_COOKIES_B64", "").strip()
PROXY_URLS = [
    value.strip()
    for value in re.split(r"[,\s]+", os.environ.get("PROXY_URLS", ""))
    if value.strip()
]
COBALT_API_URL = os.environ.get("COBALT_API_URL", "").strip().rstrip("/")
COBALT_API_KEY = os.environ.get("COBALT_API_KEY", "").strip()
BROWSER_USER_AGENT = os.environ.get(
    "BROWSER_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)


class InstagramRateLimitError(RuntimeError):
    """Instagram IP/session rate limit (HTTP 429)."""


def _prepare_cookie_file():
    """Cookie Netscape را از env رمزگذاری‌شده روی دیسک موقت می‌سازد."""
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        return COOKIES_FILE
    if not INSTAGRAM_COOKIES_B64:
        return None
    try:
        raw = base64.b64decode(INSTAGRAM_COOKIES_B64, validate=True)
        text = raw.decode("utf-8").replace("\r\n", "\n")
        if not text.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
            raise ValueError("cookie file is not in Netscape format")
        path = os.path.join(tempfile.gettempdir(), "instagram-cookies.txt")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        logger.info("کوکی Instagram از متغیر امن محیطی بارگذاری شد")
        return path
    except Exception as exc:
        logger.error("INSTAGRAM_COOKIES_B64 نامعتبر است: %s", exc)
        return None


RUNTIME_COOKIES_FILE = _prepare_cookie_file()

# ---- ضداسپم و پایداری ----
# تعداد تلاش مجدد روی خطای موقت شبکه
DOWNLOAD_RETRIES = int(os.environ.get("DOWNLOAD_RETRIES", "2"))

_user_hits = {}   # uid -> [timestamps]
_user_busy = set()  # کاربرانی که همین الان یک درخواست در حال پردازش دارند


class _DynamicLimiter:
    """محدودکننده‌ی سراسریِ داینامیک؛ سقف را هر لحظه از تنظیمات می‌خواند،
    پس ادمین می‌تواند آن را زنده از پنل تغییر دهد."""

    def __init__(self):
        self.active = 0

    async def __aenter__(self):
        while self.active >= max(1, int(_config.get("max_concurrent", 6))):
            await asyncio.sleep(0.25)
        self.active += 1
        return self

    async def __aexit__(self, *exc):
        self.active = max(0, self.active - 1)


_limiter = _DynamicLimiter()


def _rate_limited(uid):
    now = time.time()
    hits = [t for t in _user_hits.get(uid, []) if now - t < 60]
    hits.append(now)
    _user_hits[uid] = hits
    return len(hits) > max(1, int(_config.get("rate_per_min", 15)))


async def _acquire_job(update, uid):
    """کنترل محدودیت کاربر. اگر «محدودیت کاربران» خاموش باشد یا کاربر ادمین
    باشد، هیچ محدودیتی اعمال نمی‌شود و ربات تا حداکثر توان کار می‌کند."""
    if not _config.get("user_limits") or is_admin(uid):
        return True
    if _rate_limited(uid):
        await update.effective_message.reply_text(
            "⏳ تعداد درخواست‌های شما زیاد است. لطفاً یک دقیقه صبر کنید."
        )
        return False
    if uid in _user_busy:
        await update.effective_message.reply_text(
            "⏳ درخواست قبلی شما هنوز در حال پردازش است؛ کمی صبر کنید."
        )
        return False
    _user_busy.add(uid)
    return True

# محل ذخیره‌ی تنظیمات و لیست کاربران. روی هاست‌های با دیسک دائمی، DATA_DIR را
# به آن مسیر تنظیم کنید تا تنظیمات بعد از ری‌دیپلوی هم بماند.
DATA_DIR = os.environ.get("DATA_DIR", "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
USERS_PATH = os.path.join(DATA_DIR, "users.json")


def _parse_admins(raw: str):
    ids = set()
    for part in re.split(r"[,\s]+", raw or ""):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


# ادمین‌ها از متغیر محیطی (پایدار حتی بعد از ری‌دیپلوی)
ADMIN_IDS = _parse_admins(os.environ.get("ADMIN_IDS", ""))

# مقادیر پیش‌فرض تنظیمات؛ برخی از env مقداردهی اولیه می‌شوند
DEFAULT_CONFIG = {
    "maintenance": False,
    "force_join": os.environ.get("FORCE_JOIN", "false").lower() == "true",
    "channels": [
        c.strip()
        for c in re.split(r"[,\s]+", os.environ.get("REQUIRED_CHANNELS", ""))
        if c.strip()
    ],
    "services": {
        "instagram": True,
        "facebook": True,
    },
    "welcome": "به ربات دانلود و موسیقی خوش آمدید 🎉",
    # محدودیت کاربران به‌صورت پیش‌فرض خاموش است؛ از پنل قابل روشن کردن.
    "user_limits": os.environ.get("USER_LIMITS", "false").lower() == "true",
    "rate_per_min": int(os.environ.get("RATE_LIMIT_PER_MIN", "15")),
    "max_concurrent": int(os.environ.get("MAX_CONCURRENT", "6")),
}

# ---------------------------------------------------------------------------
# ذخیره‌سازی تنظیمات و کاربران
# ---------------------------------------------------------------------------
_config = dict(DEFAULT_CONFIG)
_users = set()


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_state():
    global _config, _users
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
        merged = dict(DEFAULT_CONFIG)
        merged.update(saved)
        # اطمینان از وجود همه‌ی کلیدهای services
        # فقط سرویس‌های پشتیبانی‌شده را از تنظیمات قدیمی نگه می‌داریم.
        saved_services = saved.get("services", {})
        merged["services"] = {
            key: bool(saved_services.get(key, enabled))
            for key, enabled in DEFAULT_CONFIG["services"].items()
        }
        _config = merged
    except FileNotFoundError:
        save_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("خواندن config ناموفق بود: %s", exc)

    try:
        with open(USERS_PATH, encoding="utf-8") as fh:
            _users = set(json.load(fh))
    except FileNotFoundError:
        _users = set()
    except Exception as exc:  # noqa: BLE001
        logger.warning("خواندن users ناموفق بود: %s", exc)


def save_config():
    try:
        _atomic_write(CONFIG_PATH, _config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ذخیره‌ی config ناموفق بود: %s", exc)


def save_users():
    try:
        _atomic_write(USERS_PATH, sorted(_users))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ذخیره‌ی users ناموفق بود: %s", exc)


def track_user(uid):
    if uid not in _users:
        _users.add(uid)
        save_users()


def is_admin(uid):
    return uid in ADMIN_IDS


# ---------------------------------------------------------------------------
# سرویس‌ها و منو
# ---------------------------------------------------------------------------
PLATFORM_PATTERNS = {
    # تشخیص دامنه عمداً مسیر را محدود نمی‌کند؛ Instagram و Facebook
    # شکل‌های متفاوتی برای reel/share/watch و پارامترهای اشتراک تولید می‌کنند.
    "instagram": re.compile(r"(?:instagram\.com|instagr\.am)", re.I),
    "facebook": re.compile(r"(?:facebook\.com|fb\.watch|fb\.com)", re.I),
}
URL_RE = re.compile(r"https?://[^\s<>]+", re.I)

SERVICE_LABELS = {
    "instagram": "📷 اینستاگرام",
    "facebook": "📘 فیسبوک",
}

PROMPTS = {
    "instagram": "📷 لینک پست، ریلز یا ویدیوی اینستاگرام را بفرستید.",
    "facebook": "📘 لینک پست، ریلز یا ویدیوی فیسبوک را بفرستید.",
}


def main_menu():
    rows = [
        [
            InlineKeyboardButton(SERVICE_LABELS["instagram"], callback_data="mode:instagram"),
            InlineKeyboardButton(SERVICE_LABELS["facebook"], callback_data="mode:facebook"),
        ]
    ]
    return InlineKeyboardMarkup(rows)


def _back_menu_kb():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu")]]
    )


def _prompt_kb():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ بازگشت", callback_data="menu")]]
    )


def _build_caption(title, source):
    parts = []
    if title:
        parts.append(f"🎬 {title.strip()}")
    if source:
        parts.append(f"📍 {source.strip()}")
    caption = "\n".join(parts)
    return caption[:1000] if caption else None


# ---------------------------------------------------------------------------
# دانلود (blocking) — در executor اجرا می‌شوند
# ---------------------------------------------------------------------------
def _base_ydl_opts(dest_dir):
    opts = {
        "outtmpl": os.path.join(dest_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "sleep_interval": 1,
        "max_sleep_interval": 3,
        "http_headers": {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if RUNTIME_COOKIES_FILE and os.path.exists(RUNTIME_COOKIES_FILE):
        opts["cookiefile"] = RUNTIME_COOKIES_FILE
    if PROXY_URLS:
        opts["proxy"] = random.choice(PROXY_URLS)
    return opts


def download_video(url, dest_dir):
    """فقط یک نسخه معمولی تا 720p را دانلود می‌کند."""
    last_path = None
    last_info = {}
    for cap in HEIGHT_CAPS:
        for f in os.listdir(dest_dir):
            try:
                os.remove(os.path.join(dest_dir, f))
            except OSError:
                pass
        fmt = (
            f"bv*[height<={cap}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={cap}][ext=mp4]/b[height<={cap}]/b"
        )
        opts = _base_ydl_opts(dest_dir)
        opts["format"] = fmt
        opts["merge_output_format"] = "mp4"

        info = None
        for attempt in range(1, DOWNLOAD_RETRIES + 1):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                break
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                logger.warning("دانلود (cap=%s تلاش=%s) ناموفق: %s", cap, attempt, message)
                if "HTTP Error 429" in message or "rate-limit" in message.lower():
                    # تکرار کیفیت‌های دیگر روی همان IP فقط محدودیت را شدیدتر می‌کند.
                    raise InstagramRateLimitError(message) from exc
                if attempt < DOWNLOAD_RETRIES:
                    time.sleep(min(8, 2 ** attempt))
        if info is None:
            continue

        last_info = info if isinstance(info, dict) else {}
        files = [
            os.path.join(dest_dir, f)
            for f in os.listdir(dest_dir)
            if f.lower().endswith((".mp4", ".mkv", ".webm", ".jpg", ".jpeg", ".png"))
        ]
        if not files:
            continue
        last_path = max(files, key=os.path.getsize)
        if os.path.getsize(last_path) <= MAX_TELEGRAM_BYTES or last_path.lower().endswith(
            (".jpg", ".jpeg", ".png")
        ):
            title = last_info.get("title") or ""
            source = last_info.get("uploader") or last_info.get("extractor_key") or ""
            return last_path, title, source
    if last_path and last_path.lower().endswith((".jpg", ".jpeg", ".png")):
        return last_path, last_info.get("title", ""), last_info.get("uploader", "")
    return None


def _cobalt_extension(filename, content_type):
    """پسوند امن رسانه را از پاسخ Cobalt تعیین می‌کند."""
    ext = os.path.splitext((filename or "").split("?", 1)[0])[1].lower()
    if ext in (".mp4", ".webm", ".mkv", ".jpg", ".jpeg", ".png"):
        return ext
    ctype = (content_type or "").lower()
    if "image/jpeg" in ctype:
        return ".jpg"
    if "image/png" in ctype:
        return ".png"
    if "video/webm" in ctype:
        return ".webm"
    return ".mp4"


def download_via_cobalt(url, dest_dir):
    """مسیر پشتیبان مستقل: دریافت یک نسخه معمولی از Cobalt خودمیزبان."""
    if not COBALT_API_URL:
        return None

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": BROWSER_USER_AGENT,
    }
    if COBALT_API_KEY:
        headers["Authorization"] = f"Api-Key {COBALT_API_KEY}"

    payload = {
        "url": url,
        "videoQuality": "720",
        "downloadMode": "auto",
        "filenameStyle": "basic",
        "alwaysProxy": True,
    }
    response = requests.post(
        f"{COBALT_API_URL}/",
        json=payload,
        headers=headers,
        timeout=(20, 150),
    )
    response.raise_for_status()
    data = response.json()
    status = data.get("status")

    media_url = None
    filename = data.get("filename") or "media"
    if status in ("tunnel", "redirect"):
        media_url = data.get("url")
    elif status == "picker":
        items = data.get("picker") or []
        chosen = next((item for item in items if item.get("type") == "video"), None)
        chosen = chosen or next((item for item in items if item.get("url")), None)
        if chosen:
            media_url = chosen.get("url")
            filename = chosen.get("filename") or filename
    elif status == "error":
        error = data.get("error") or {}
        raise RuntimeError(f"Cobalt: {error.get('code') or 'download failed'}")

    if not media_url:
        raise RuntimeError(f"Cobalt response unsupported: {status}")

    download_headers = {"User-Agent": BROWSER_USER_AGENT}
    if COBALT_API_KEY and media_url.startswith(COBALT_API_URL):
        download_headers["Authorization"] = f"Api-Key {COBALT_API_KEY}"
    with requests.get(
        media_url,
        headers=download_headers,
        stream=True,
        allow_redirects=True,
        timeout=(20, 180),
    ) as media:
        media.raise_for_status()
        declared = int(media.headers.get("Content-Length") or 0)
        if declared > MAX_TELEGRAM_BYTES:
            raise RuntimeError("Cobalt media is larger than Telegram limit")
        ext = _cobalt_extension(filename, media.headers.get("Content-Type"))
        path = os.path.join(dest_dir, f"cobalt_media{ext}")
        size = 0
        with open(path, "wb") as handle:
            for chunk in media.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_TELEGRAM_BYTES:
                    raise RuntimeError("Cobalt media exceeded Telegram limit")
                handle.write(chunk)

    if size <= 0:
        raise RuntimeError("Cobalt returned an empty media file")
    title = os.path.splitext(os.path.basename(filename))[0]
    source = SERVICE_LABELS.get(detect_platform(url), "").replace("📷 ", "").replace("📘 ", "")
    return path, title, source


def download_media(url, dest_dir):
    """موتور اصلی، سپس fallback مستقل؛ جزئیات شکست فقط در لاگ می‌ماند."""
    try:
        result = download_video(url, dest_dir)
        if result:
            return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("موتور اصلی ناموفق شد؛ انتقال به fallback: %s", exc)

    if not COBALT_API_URL:
        return None
    for name in os.listdir(dest_dir):
        try:
            os.remove(os.path.join(dest_dir, name))
        except OSError:
            pass
    try:
        return download_via_cobalt(url, dest_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("موتور Cobalt ناموفق شد: %s", exc)
        return None


def extract_audio_track(media_path, dest_dir):
    """صدای ویدیو را به MP3 استاندارد تلگرام تبدیل می‌کند."""
    output = os.path.join(dest_dir, "audio.mp3")
    command = [
        "ffmpeg", "-y", "-i", media_path, "-vn",
        "-acodec", "libmp3lame", "-b:a", "192k", "-ar", "44100", output,
    ]
    try:
        completed = subprocess.run(
            command, check=False, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=180,
        )
        if completed.returncode != 0 or not os.path.exists(output):
            logger.warning("استخراج صدا ناموفق بود: %s", completed.stderr[-500:])
            return None
        if os.path.getsize(output) <= 0 or os.path.getsize(output) > MAX_TELEGRAM_BYTES:
            return None
        return output
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("خطا در FFmpeg: %s", exc)
        return None


def download_full_song(title, artist, dest_dir):
    """نسخه کامل آهنگ شناسایی‌شده را پیدا و به MP3 تبدیل می‌کند."""
    query = " ".join(part for part in (title, artist) if part).strip()
    if not query:
        return None
    opts = _base_ydl_opts(dest_dir)
    opts.update({
        "outtmpl": os.path.join(dest_dir, "full_%(id)s.%(ext)s"),
        "format": "bestaudio/best",
        "default_search": "ytsearch1",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    })
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=True)
        if isinstance(info, dict) and info.get("entries"):
            info = next((entry for entry in info["entries"] if entry), {})
        candidates = [
            os.path.join(dest_dir, name)
            for name in os.listdir(dest_dir)
            if name.startswith("full_") and name.lower().endswith(".mp3")
        ]
        if not candidates:
            return None
        path = max(candidates, key=os.path.getsize)
        if os.path.getsize(path) <= 0 or os.path.getsize(path) > MAX_TELEGRAM_BYTES:
            return None
        found_title = (info or {}).get("track") or (info or {}).get("title") or title
        found_artist = (info or {}).get("artist") or (info or {}).get("uploader") or artist
        return path, found_title, found_artist
    except Exception as exc:
        logger.warning("دریافت نسخه کامل آهنگ ناموفق بود: %s", exc)
        return None


async def recognize_song(audio_path):
    if not _SHAZAM_AVAILABLE:
        return None
    try:
        out = await Shazam().recognize(audio_path)
        track = out.get("track")
        if not track:
            return None
        return track.get("title", ""), track.get("subtitle", "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("تشخیص آهنگ ناموفق: %s", exc)
        return None


async def _send_media_file(context, chat_id, path, caption=None):
    lower = path.lower()
    if lower.endswith((".jpg", ".jpeg", ".png")):
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
        with open(path, "rb") as fh:
            await context.bot.send_photo(chat_id, photo=fh, caption=caption)
    elif lower.endswith(".mp3"):
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
        with open(path, "rb") as fh:
            await context.bot.send_audio(chat_id, audio=fh, caption=caption)
    else:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
        with open(path, "rb") as fh:
            await context.bot.send_video(
                chat_id, video=fh, caption=caption, supports_streaming=True
            )


# ---------------------------------------------------------------------------
# قفل عضویت اجباری
# ---------------------------------------------------------------------------
async def is_member_all(context, uid):
    """آیا کاربر عضو همه‌ی کانال‌های اجباری است؟ (خطا ⇒ عبور می‌دهیم)"""
    for ch in _config.get("channels", []):
        try:
            member = await context.bot.get_chat_member(ch, uid)
            if member.status in ("left", "kicked"):
                return False
        except TelegramError as exc:
            logger.warning("بررسی عضویت %s ناموفق (ربات ادمین کانال هست؟): %s", ch, exc)
            continue  # fail-open تا کاربران قفل نشوند
    return True


def _join_keyboard():
    rows = []
    for ch in _config.get("channels", []):
        uname = ch.lstrip("@")
        rows.append([InlineKeyboardButton(f"📢 عضویت در {ch}", url=f"https://t.me/{uname}")])
    rows.append([InlineKeyboardButton("✅ عضو شدم", callback_data="checkjoin")])
    return InlineKeyboardMarkup(rows)


async def require_membership(update, context, uid):
    if not _config.get("force_join") or not _config.get("channels"):
        return True
    if is_admin(uid):
        return True
    if await is_member_all(context, uid):
        return True
    text = "🔒 برای استفاده از ربات، اول در کانال‌های زیر عضو شوید و بعد «✅ عضو شدم» را بزنید:"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=_join_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=_join_keyboard())
    return False


async def on_checkjoin(update, context):
    query = update.callback_query
    uid = query.from_user.id
    if await is_member_all(context, uid):
        await query.answer("عضویت تأیید شد ✅")
        await query.edit_message_text("✅ عضویت تأیید شد! حالا یک سرویس انتخاب کنید:",
                                      reply_markup=main_menu())
    else:
        await query.answer("هنوز عضو همه‌ی کانال‌ها نیستید ❌", show_alert=True)


# ---------------------------------------------------------------------------
# دستورهای عمومی
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    track_user(uid)
    context.user_data.pop("mode", None)
    context.user_data.pop("await", None)
    if _config.get("maintenance") and not is_admin(uid):
        await update.message.reply_text("🚧 ربات موقتاً در حال تعمیر است. کمی بعد امتحان کنید.")
        return
    if not await require_membership(update, context, uid):
        return
    await update.message.reply_text(
        f"👋 {_config.get('welcome')}\n\nیک سرویس انتخاب کنید 👇",
        reply_markup=main_menu(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "کافی است لینک عمومی اینستاگرام یا فیسبوک را بفرستید.\n"
        "ربات ویدیو، فایل MP3 و نتیجه تشخیص آهنگ را برایتان ارسال می‌کند."
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 آی‌دی عددی شما: `{update.effective_user.id}`",
                                    parse_mode="Markdown")


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    mode = query.data.split(":", 1)[1]
    if _config.get("maintenance") and not is_admin(uid):
        await query.answer("🚧 ربات در حال تعمیر است.", show_alert=True)
        return
    if not _config["services"].get(mode, True):
        await query.answer("این سرویس موقتاً غیرفعال است.", show_alert=True)
        return
    if not await require_membership(update, context, uid):
        await query.answer()
        return
    await query.answer()
    context.user_data["mode"] = mode
    await query.edit_message_text(PROMPTS.get(mode, "لینک را بفرستید."),
                                  reply_markup=_prompt_kb())


async def on_menu_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("mode", None)
    await query.edit_message_text("یک سرویس انتخاب کنید 👇", reply_markup=main_menu())


def detect_platform(text):
    for name, pat in PLATFORM_PATTERNS.items():
        if pat.search(text):
            return name
    return None


# ---------------------------------------------------------------------------
# پیام‌های متنی و صوتی کاربر
# ---------------------------------------------------------------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    track_user(uid)
    text = (update.message.text or "").strip()

    # حالت‌های ورودی ادمین (پیام همگانی، پیام خوش‌آمد، افزودن کانال)
    if is_admin(uid) and context.user_data.get("await"):
        await _handle_admin_input(update, context, text)
        return

    if _config.get("maintenance") and not is_admin(uid):
        await update.message.reply_text("🚧 ربات موقتاً در حال تعمیر است.")
        return
    if not await require_membership(update, context, uid):
        return

    mode = context.user_data.get("mode")
    url_match = URL_RE.search(text)

    if url_match:
        platform = detect_platform(text) or mode
        if platform in _config["services"] and not _config["services"][platform]:
            await update.message.reply_text("این سرویس موقتاً غیرفعال است.")
            return
        await _do_video_download(update, context, url_match.group(0))
        return

    if mode in ("instagram", "facebook"):
        await update.message.reply_text("❌ لطفاً یک لینک معتبر بفرستید.")
    else:
        await update.message.reply_text(
            "لطفاً اول با /start یک سرویس انتخاب کنید یا یک لینک بفرستید.",
            reply_markup=main_menu(),
        )


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تشخیص آهنگ از فایل صوتی/ویدیویی که کاربر مستقیماً می‌فرستد."""
    uid = update.effective_user.id
    track_user(uid)
    if _config.get("maintenance") and not is_admin(uid):
        await update.message.reply_text("🚧 ربات موقتاً در حال تعمیر است.")
        return
    if not await require_membership(update, context, uid):
        return
    if not _SHAZAM_AVAILABLE:
        await update.message.reply_text("❌ سرویس تشخیص آهنگ موقتاً در دسترس نیست.")
        return
    if not await _acquire_job(update, uid):
        return

    status = await update.message.reply_text("🎧 در حال شنیدن و تشخیص آهنگ...")
    tmpdir = tempfile.mkdtemp(prefix="recognize_")
    try:
        msg = update.message
        media = msg.voice or msg.audio or msg.video or msg.video_note
        if not media:
            await status.edit_text("❌ فایل قابل پردازشی پیدا نشد.")
            return
        tg_file = await media.get_file()
        source = os.path.join(tmpdir, "sample")
        await tg_file.download_to_drive(source)
        async with _limiter:
            result = await recognize_song(source)
        if result and result[0]:
            title, artist = result
            await status.edit_text(
                f"🎵 آهنگ پیدا شد!\n\nعنوان: {title}\nخواننده: {artist or 'نامشخص'}"
                "\n\n⏳ در حال یافتن نسخه کامل..."
            )
            loop = asyncio.get_running_loop()
            async with _limiter:
                full_song = await loop.run_in_executor(
                    None, download_full_song, title, artist, tmpdir
                )
            if full_song:
                song_path, found_title, found_artist = full_song
                caption = f"🎵 {title}"
                if artist:
                    caption += f" — {artist}"
                await _send_media_file(context, update.effective_chat.id, song_path, caption)
                await status.edit_text(
                    "✅ نسخه کامل آهنگ ارسال شد.",
                    reply_markup=_back_menu_kb(),
                )
            else:
                await status.edit_text(
                    f"🎵 آهنگ شناسایی شد:\n{title} — {artist or 'نامشخص'}"
                    "\n\n❌ نسخه کامل قابل دریافت نبود.",
                    reply_markup=_back_menu_kb(),
                )
        else:
            await status.edit_text(
                "🔍 آهنگ تشخیص داده نشد. یک بخش واضح‌تر ۱۰ تا ۳۰ ثانیه‌ای بفرستید.",
                reply_markup=_back_menu_kb(),
            )
    except Exception as exc:
        logger.exception("خطا در تشخیص فایل کاربر: %s", exc)
        await status.edit_text("❌ پردازش فایل ناموفق بود؛ دوباره تلاش کنید.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        _user_busy.discard(uid)


async def _do_video_download(update, context, url):
    """دانلود رسانه، ارسال ویدیو، استخراج MP3 و تشخیص موسیقی در یک گردش کار."""
    uid = update.effective_user.id
    platform = detect_platform(url)
    if not platform:
        await update.message.reply_text(
            "❌ فقط لینک معتبر Instagram یا Facebook پذیرفته می‌شود.",
            reply_markup=main_menu(),
        )
        return
    if not _config["services"].get(platform, True):
        await update.message.reply_text("این سرویس موقتاً غیرفعال است.")
        return
    if not await _acquire_job(update, uid):
        return

    chat_id = update.effective_chat.id
    status = await update.message.reply_text(
        f"⏳ لینک {SERVICE_LABELS[platform]} دریافت شد؛ در حال آماده‌سازی رسانه..."
    )
    tmpdir = tempfile.mkdtemp(prefix=f"{platform}_")
    try:
        loop = asyncio.get_running_loop()
        async with _limiter:
            await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
            result = await loop.run_in_executor(None, download_media, url, tmpdir)
            if not result:
                await status.edit_text(
                    "❌ دانلود انجام نشد. محتوا باید عمومی و لینک آن معتبر باشد.",
                    reply_markup=_back_menu_kb(),
                )
                return

            path, title, source = result
            audio_path = None
            song = None
            full_song = None
            if path.lower().endswith((".mp4", ".mkv", ".webm")):
                audio_path = await loop.run_in_executor(
                    None, extract_audio_track, path, tmpdir
                )
                if audio_path:
                    song = await recognize_song(audio_path)
                    if song and song[0]:
                        full_song = await loop.run_in_executor(
                            None, download_full_song, song[0], song[1], tmpdir
                        )

        await status.edit_text("📤 دانلود کامل شد؛ در حال ارسال ویدیو...")
        await _send_media_file(context, chat_id, path, _build_caption(title, source))

        if full_song:
            song_path, found_title, found_artist = full_song
            music_caption = f"🎵 {song[0]}"
            if song[1]:
                music_caption += f" — {song[1]}"
            await _send_media_file(context, chat_id, song_path, music_caption)
            final = (
                "✅ ویدیو و نسخه کامل آهنگ ارسال شد.\n\n"
                f"🎵 آهنگ: {song[0]}\n"
                f"🎤 خواننده: {song[1] or 'نامشخص'}"
            )
        elif song and song[0]:
            final = (
                "✅ ویدیو ارسال شد.\n\n"
                f"🎵 آهنگ شناسایی شد: {song[0]} — {song[1] or 'نامشخص'}\n"
                "❌ نسخه کامل قابل دریافت نبود."
            )
        elif audio_path:
            final = "✅ ویدیو ارسال شد؛ نام آهنگ از این بخش قابل تشخیص نبود."
        else:
            final = "✅ رسانه ارسال شد؛ این فایل صدای قابل تشخیص نداشت."

        await status.edit_text(final, reply_markup=_back_menu_kb())
    except InstagramRateLimitError:
        logger.warning("Instagram درخواست این IP را با 429 محدود کرد")
        await status.edit_text(
            "❌ در حال حاضر دریافت این رسانه ممکن نشد. لطفاً کمی بعد دوباره تلاش کنید.",
            reply_markup=_back_menu_kb(),
        )
    except Exception as exc:
        logger.exception("خطا در پردازش لینک %s: %s", platform, exc)
        try:
            await status.edit_text(
                "❌ پردازش ناموفق بود. چند لحظه بعد دوباره تلاش کنید.",
                reply_markup=_back_menu_kb(),
            )
        except Exception:
            pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        _user_busy.discard(uid)


# ---------------------------------------------------------------------------
# پنل مدیریت
# ---------------------------------------------------------------------------
PANEL_TITLE = "🛠 *پنل مدیریت*\nهمه‌ی تنظیمات زنده اعمال می‌شوند."
LIMITS_TEXT = (
    "⚙️ *محدودیت‌ها و کارایی*\n\n"
    "👤 *محدودیت کاربران*: وقتی خاموش باشد، ربات بدون هیچ محدودیتی به همه‌ی "
    "کاربران سرویس می‌دهد (پیش‌فرض). روشنش کنید تا نرخ درخواست هر کاربر کنترل شود.\n\n"
    "📥 *دانلود همزمان*: سقف کل سرور. بالاتر = سریع‌تر برای همه، ولی مصرف منابع "
    "بیشتر (اگر هاست ضعیف است زیاد بالا نبرید)."
)


def _onoff(b):
    return "🟢" if b else "🔴"


def _state(b):
    return "روشن ✅" if b else "خاموش ⛔️"


def admin_panel():
    c = _config
    rows = [
        [InlineKeyboardButton("📊  آمار و وضعیت", callback_data="adm:stats")],
        [InlineKeyboardButton("🧩  مدیریت سرویس‌ها", callback_data="adm:services")],
        [InlineKeyboardButton(f"🔒  قفل عضویت اجباری — {_state(c['force_join'])}",
                              callback_data="adm:toggle:force_join")],
        [InlineKeyboardButton("📢  مدیریت کانال‌ها", callback_data="adm:channels")],
        [InlineKeyboardButton("⚙️  محدودیت‌ها و کارایی", callback_data="adm:limits")],
        [InlineKeyboardButton(f"🚧  حالت تعمیر — {_state(c['maintenance'])}",
                              callback_data="adm:toggle:maintenance")],
        [InlineKeyboardButton("✉️  ارسال پیام همگانی", callback_data="adm:broadcast")],
        [InlineKeyboardButton("✏️  ویرایش پیام خوش‌آمد", callback_data="adm:welcome")],
    ]
    return InlineKeyboardMarkup(rows)


def limits_panel():
    c = _config
    rows = [
        [InlineKeyboardButton(f"👤  محدودیت کاربران — {_state(c.get('user_limits'))}",
                              callback_data="adm:toggle:user_limits")],
        [
            InlineKeyboardButton("➖", callback_data="adm:lim:rate:dec"),
            InlineKeyboardButton(f"نرخ هر کاربر: {c.get('rate_per_min')}/دقیقه",
                                 callback_data="adm:noop"),
            InlineKeyboardButton("➕", callback_data="adm:lim:rate:inc"),
        ],
        [
            InlineKeyboardButton("➖", callback_data="adm:lim:conc:dec"),
            InlineKeyboardButton(f"دانلود همزمان: {c.get('max_concurrent')}",
                                 callback_data="adm:noop"),
            InlineKeyboardButton("➕", callback_data="adm:lim:conc:inc"),
        ],
        [InlineKeyboardButton("⬅️  بازگشت", callback_data="adm:home")],
    ]
    return InlineKeyboardMarkup(rows)


def services_panel():
    onoff = lambda b: "🟢" if b else "🔴"  # noqa: E731
    rows = [
        [InlineKeyboardButton(
            f"{onoff(_config['services'][k])} {SERVICE_LABELS[k]}",
            callback_data=f"adm:svc:{k}")]
        for k in ("instagram", "facebook")
    ]
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="adm:home")])
    return InlineKeyboardMarkup(rows)


def channels_panel():
    rows = []
    for ch in _config.get("channels", []):
        rows.append([InlineKeyboardButton(f"❌ حذف {ch}", callback_data=f"adm:delch:{ch}")])
    rows.append([InlineKeyboardButton("➕ افزودن کانال", callback_data="adm:addch")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="adm:home")])
    return InlineKeyboardMarkup(rows)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(
            "⛔️ شما ادمین نیستید.\n"
            f"آی‌دی عددی شما: `{uid}`\n"
            "این آی‌دی را در متغیر محیطی ADMIN_IDS هاست بگذارید تا ادمین شوید.",
            parse_mode="Markdown",
        )
        return
    await update.message.reply_text(PANEL_TITLE, parse_mode="Markdown",
                                    reply_markup=admin_panel())


async def on_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if not is_admin(uid):
        await query.answer("⛔️ دسترسی ندارید.", show_alert=True)
        return
    data = query.data[len("adm:"):]

    if data == "home":
        await query.answer()
        await query.edit_message_text(PANEL_TITLE, parse_mode="Markdown",
                                      reply_markup=admin_panel())

    elif data == "noop":
        await query.answer()

    elif data == "stats":
        await query.answer()
        c = _config
        svc = c["services"]
        txt = (
            "📊 *آمار و وضعیت*\n\n"
            f"👥 کاربران: {len(_users)}\n"
            f"📥 دانلودهای فعال: {_limiter.active}\n\n"
            f"🔒 قفل عضویت: {'روشن' if c['force_join'] else 'خاموش'}\n"
            f"📢 کانال‌ها: {', '.join(c['channels']) or '—'}\n"
            f"🚧 حالت تعمیر: {'روشن' if c['maintenance'] else 'خاموش'}\n"
            f"👤 محدودیت کاربران: {'روشن' if c['user_limits'] else 'خاموش'} "
            f"({c['rate_per_min']}/دقیقه)\n"
            f"📥 سقف دانلود همزمان: {c['max_concurrent']}\n\n"
            "🧩 سرویس‌ها:\n"
            f"  اینستاگرام {_onoff(svc['instagram'])}  فیسبوک {_onoff(svc['facebook'])}\n"
            "  🎵 استخراج و تشخیص موسیقی: خودکار برای هر ویدیو\n"
        )
        await query.edit_message_text(
            txt, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ بازگشت", callback_data="adm:home")]]),
        )

    elif data == "limits":
        await query.answer()
        await query.edit_message_text(LIMITS_TEXT, parse_mode="Markdown",
                                      reply_markup=limits_panel())

    elif data.startswith("lim:"):
        _, field, op = data.split(":")
        if field == "rate":
            step = 5 if op == "inc" else -5
            _config["rate_per_min"] = max(1, int(_config.get("rate_per_min", 15)) + step)
        else:  # conc
            step = 1 if op == "inc" else -1
            _config["max_concurrent"] = max(1, min(50, int(_config.get("max_concurrent", 6)) + step))
        save_config()
        await query.answer("تغییر کرد ✅")
        await query.edit_message_text(LIMITS_TEXT, parse_mode="Markdown",
                                      reply_markup=limits_panel())

    elif data.startswith("toggle:"):
        key = data.split(":", 1)[1]
        _config[key] = not _config.get(key, False)
        save_config()
        await query.answer("تغییر کرد ✅")
        if key == "user_limits":
            await query.edit_message_text(LIMITS_TEXT, parse_mode="Markdown",
                                          reply_markup=limits_panel())
        else:
            await query.edit_message_text(PANEL_TITLE, parse_mode="Markdown",
                                          reply_markup=admin_panel())

    elif data == "services":
        await query.answer()
        await query.edit_message_text("🧩 روشن/خاموش کردن سرویس‌ها:", reply_markup=services_panel())

    elif data.startswith("svc:"):
        key = data.split(":", 1)[1]
        _config["services"][key] = not _config["services"].get(key, True)
        save_config()
        await query.answer("تغییر کرد ✅")
        await query.edit_message_text("🧩 روشن/خاموش کردن سرویس‌ها:", reply_markup=services_panel())

    elif data == "channels":
        await query.answer()
        await query.edit_message_text(
            "📢 کانال‌های عضویت اجباری:", reply_markup=channels_panel())

    elif data == "addch":
        await query.answer()
        context.user_data["await"] = "addch"
        await query.edit_message_text(
            "یوزرنیم کانال را بفرستید (مثلاً `@mychannel`).\n"
            "⚠️ ربات باید در آن کانال ادمین باشد تا بتواند عضویت را بررسی کند.",
            parse_mode="Markdown",
        )

    elif data.startswith("delch:"):
        ch = data.split(":", 1)[1]
        if ch in _config["channels"]:
            _config["channels"].remove(ch)
            save_config()
        await query.answer("حذف شد ✅")
        await query.edit_message_text("📢 کانال‌های عضویت اجباری:", reply_markup=channels_panel())

    elif data == "broadcast":
        await query.answer()
        context.user_data["await"] = "broadcast"
        await query.edit_message_text(
            "پیامی که می‌خواهید برای همه‌ی کاربران ارسال شود را بفرستید.\n"
            "برای لغو /cancel را بزنید.")

    elif data == "welcome":
        await query.answer()
        context.user_data["await"] = "welcome"
        await query.edit_message_text(
            "متن جدید پیام خوش‌آمد را بفرستید.\nبرای لغو /cancel را بزنید.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("await", None)
    await update.message.reply_text("لغو شد.")


async def _handle_admin_input(update, context, text):
    mode = context.user_data.pop("await", None)
    if mode == "welcome":
        _config["welcome"] = text
        save_config()
        await update.message.reply_text("✅ پیام خوش‌آمد به‌روزرسانی شد.",
                                        reply_markup=admin_panel())
    elif mode == "addch":
        ch = text.strip()
        if not ch.startswith("@"):
            ch = "@" + ch.lstrip("@")
        if ch not in _config["channels"]:
            _config["channels"].append(ch)
            save_config()
        await update.message.reply_text(f"✅ کانال {ch} اضافه شد.",
                                        reply_markup=channels_panel())
    elif mode == "broadcast":
        await _broadcast(update, context, text)


async def _broadcast(update, context, text):
    total = len(_users)
    status = await update.message.reply_text(f"📤 در حال ارسال به {total} کاربر...")
    ok = fail = 0
    for uid in list(_users):
        try:
            await context.bot.send_message(uid, text)
            ok += 1
        except Exception:  # noqa: BLE001
            fail += 1
        await asyncio.sleep(0.05)  # جلوگیری از محدودیت فلود تلگرام
    await status.edit_text(f"✅ ارسال شد: {ok} موفق، {fail} ناموفق.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("خطا هنگام پردازش آپدیت:", exc_info=context.error)


# ---------------------------------------------------------------------------
# اجرا
# ---------------------------------------------------------------------------
async def _post_init(app):
    # دستورهای عمومی (برای همه) — بدون /admin تا پنل مخفی بماند
    public = [
        BotCommand("start", "شروع / منوی اصلی"),
        BotCommand("help", "راهنما"),
        BotCommand("id", "نمایش آی‌دی عددی من"),
    ]
    await app.bot.set_my_commands(public, scope=BotCommandScopeDefault())
    # دستورهای ادمین (شامل /admin) فقط برای ادمین‌ها نمایش داده می‌شود
    admin_cmds = public + [BotCommand("admin", "پنل مدیریت")]
    for aid in ADMIN_IDS:
        try:
            await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(aid))
        except Exception as exc:  # noqa: BLE001
            logger.warning("تنظیم دستورهای ادمین برای %s ناموفق: %s", aid, exc)


def main():
    if not BOT_TOKEN:
        raise SystemExit("متغیر محیطی BOT_TOKEN تنظیم نشده است.")

    load_state()
    logger.info("ادمین‌ها: %s | کاربران ذخیره‌شده: %d", ADMIN_IDS or "—", len(_users))

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", myid))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(on_menu_back, pattern=r"^menu$"))
    app.add_handler(CallbackQueryHandler(on_checkjoin, pattern=r"^checkjoin$"))
    app.add_handler(CallbackQueryHandler(on_admin, pattern=r"^adm:"))
    app.add_handler(
        MessageHandler(
            filters.VOICE | filters.AUDIO | filters.VIDEO | filters.VIDEO_NOTE,
            handle_audio,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)

    if WEBHOOK_URL:
        logger.info("اجرا در حالت webhook روی پورت %s", PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
            drop_pending_updates=True,
        )
    else:
        logger.info("اجرا در حالت polling")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
