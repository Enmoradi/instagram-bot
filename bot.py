"""
ربات چندمنظوره‌ی دانلود و موسیقی + پنل مدیریت کامل
-------------------------------------------------
سرویس‌ها:  📷 اینستاگرام | 📘 فیسبوک | ▶️ یوتیوب | 🎵 موسیقی

قابلیت‌ها:
  - دانلود ویدیو/عکس از اینستاگرام، فیسبوک، یوتیوب (yt-dlp)
  - انتخاب خودکار بهترین کیفیت زیر ۵۰ مگابایت
  - موسیقی: جستجو با نام + تشخیص آهنگ از روی کلیپ (Shazam)
  - حذف کامل فایل‌ها بعد از ارسال (مصرف حافظه ≈ صفر)
  - پردازش همزمان برای تعداد کاربر بالا
  - پنل مدیریت داینامیک: قفل عضویت اجباری، فعال/غیرفعال کردن سرویس‌ها،
    حالت تعمیر، پیام همگانی، آمار، پیام خوش‌آمد
"""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time

from telegram import (
    BotCommand,
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
MAX_TELEGRAM_BYTES = 50 * 1024 * 1024
HEIGHT_CAPS = [1080, 720, 480, 360, 240]
COOKIES_FILE = os.environ.get("COOKIES_FILE")

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
        "youtube": True,
        "music": True,
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
        svc = dict(DEFAULT_CONFIG["services"])
        svc.update(saved.get("services", {}))
        merged["services"] = svc
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
    "instagram": re.compile(r"instagram\.com|instagr\.am", re.I),
    "facebook": re.compile(r"facebook\.com|fb\.watch|fb\.com", re.I),
    "youtube": re.compile(r"youtube\.com|youtu\.be", re.I),
}
URL_RE = re.compile(r"https?://\S+", re.I)

SERVICE_LABELS = {
    "instagram": "📷 اینستاگرام",
    "facebook": "📘 فیسبوک",
    "youtube": "▶️ یوتیوب",
    "music": "🎵 موسیقی",
}

PROMPTS = {
    "instagram": "📷 لینک پست یا ریلز اینستاگرام را بفرستید.",
    "facebook": "📘 لینک ویدیوی فیسبوک را بفرستید.",
    "youtube": "▶️ لینک ویدیوی یوتیوب را بفرستید.",
    "music": (
        "🎵 حالت موسیقی:\n\n"
        "• نام آهنگ یا خواننده را تایپ کنید.\n"
        "• یا یک پیام صوتی/ویدیو بفرستید تا آهنگش را تشخیص دهم."
    ),
}


def main_menu():
    rows, row = [], []
    for key in ("instagram", "facebook", "youtube", "music"):
        if _config["services"].get(key, True):
            row.append(InlineKeyboardButton(SERVICE_LABELS[key], callback_data=f"mode:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows) if rows else None


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
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def download_video(url, dest_dir):
    """خروجی: (مسیر, عنوان, منبع) زیر ۵۰MB، یا None."""
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
                logger.warning("دانلود (cap=%s تلاش=%s) ناموفق: %s", cap, attempt, exc)
                if attempt < DOWNLOAD_RETRIES:
                    time.sleep(1.5)
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


def download_audio_by_query(query, dest_dir):
    opts = _base_ydl_opts(dest_dir)
    opts["format"] = "bestaudio/best"
    opts["postprocessors"] = [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
    ]
    target = query if URL_RE.search(query) else f"ytsearch1:{query}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("دانلود موسیقی ناموفق: %s", exc)
        return None
    if "entries" in info:
        info = info["entries"][0] if info["entries"] else None
    if not info:
        return None
    mp3s = [
        os.path.join(dest_dir, f)
        for f in os.listdir(dest_dir)
        if f.lower().endswith(".mp3")
    ]
    if not mp3s or os.path.getsize(mp3s[0]) > MAX_TELEGRAM_BYTES:
        return None
    title = info.get("track") or info.get("title") or "Unknown"
    artist = info.get("artist") or info.get("uploader") or ""
    return mp3s[0], title, artist


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
        "برای شروع /start را بزنید و یک سرویس انتخاب کنید،\n"
        "یا مستقیم لینک اینستاگرام/فیسبوک/یوتیوب را بفرستید."
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

    if mode == "music":
        if not _config["services"].get("music", True):
            await update.message.reply_text("این سرویس موقتاً غیرفعال است.")
            return
        await _do_music_search(update, context, text)
        return

    if url_match:
        platform = detect_platform(text) or mode
        if platform in _config["services"] and not _config["services"][platform]:
            await update.message.reply_text("این سرویس موقتاً غیرفعال است.")
            return
        await _do_video_download(update, context, url_match.group(0))
        return

    if mode in ("instagram", "facebook", "youtube"):
        await update.message.reply_text("❌ لطفاً یک لینک معتبر بفرستید.")
    else:
        await update.message.reply_text(
            "لطفاً اول با /start یک سرویس انتخاب کنید یا یک لینک بفرستید.",
            reply_markup=main_menu(),
        )


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    track_user(uid)
    if _config.get("maintenance") and not is_admin(uid):
        await update.message.reply_text("🚧 ربات موقتاً در حال تعمیر است.")
        return
    if not await require_membership(update, context, uid):
        return
    if not _config["services"].get("music", True):
        await update.message.reply_text("سرویس موسیقی موقتاً غیرفعال است.")
        return
    if not _SHAZAM_AVAILABLE:
        await update.message.reply_text("❌ قابلیت تشخیص آهنگ روی این سرور فعال نیست.")
        return
    if not await _acquire_job(update, uid):
        return

    chat_id = update.effective_chat.id
    status = await update.message.reply_text("🎧 در حال تشخیص آهنگ...")
    tmpdir = tempfile.mkdtemp(prefix="rec_")
    try:
        async with _limiter:
            msg = update.message
            tg_file = None
            for attr in ("voice", "audio", "video", "video_note"):
                media = getattr(msg, attr, None)
                if media:
                    tg_file = await media.get_file()
                    break
            if tg_file is None:
                await status.edit_text("❌ فایل صوتی پیدا نشد.")
                return
            src = os.path.join(tmpdir, "input")
            await tg_file.download_to_drive(src)

            result = await recognize_song(src)
            if not result or not result[0]:
                await status.edit_text(
                    "❌ آهنگ تشخیص داده نشد. کلیپ واضح‌تری بفرستید.",
                    reply_markup=_back_menu_kb(),
                )
                return
            title, artist = result
            await status.edit_text(
                f"🎵 پیدا شد:\n*{title}* — {artist}\n\n⏳ در حال ارسال...",
                parse_mode="Markdown",
            )
            loop = asyncio.get_running_loop()
            got = await loop.run_in_executor(
                None, download_audio_by_query, f"{title} {artist}", tmpdir
            )
        if got:
            await _send_media_file(context, chat_id, got[0], _build_caption(title, artist))
            await status.edit_text(f"✅ {title} — {artist}", reply_markup=_back_menu_kb())
        else:
            await status.edit_text(
                f"🎵 شناسایی شد:\n*{title}* — {artist}\nولی فایلش پیدا نشد.",
                parse_mode="Markdown",
                reply_markup=_back_menu_kb(),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("خطا در تشخیص: %s", exc)
        try:
            await status.edit_text("❌ خطا در پردازش. دوباره تلاش کنید.")
        except Exception:  # noqa: BLE001
            pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        _user_busy.discard(uid)


async def _do_video_download(update, context, url):
    uid = update.effective_user.id
    if not await _acquire_job(update, uid):
        return
    chat_id = update.effective_chat.id
    status = await update.message.reply_text("⏳ در حال دانلود... کمی صبر کنید.")
    tmpdir = tempfile.mkdtemp(prefix="dl_")
    try:
        loop = asyncio.get_running_loop()
        async with _limiter:
            await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
            result = await loop.run_in_executor(None, download_video, url, tmpdir)
        if not result:
            await status.edit_text(
                "❌ دانلود ناموفق بود.\nشاید محتوا خصوصی است یا حتی کم‌کیفیت‌ترین نسخه هم از ۵۰MB بزرگ‌تر است.",
                reply_markup=_back_menu_kb(),
            )
            return
        path, title, source = result
        await _send_media_file(context, chat_id, path, _build_caption(title, source))
        await status.edit_text("✅ انجام شد!", reply_markup=_back_menu_kb())
    except Exception as exc:  # noqa: BLE001
        logger.exception("خطا در دانلود: %s", exc)
        try:
            await status.edit_text("❌ خطای غیرمنتظره. دوباره تلاش کنید.")
        except Exception:  # noqa: BLE001
            pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        _user_busy.discard(uid)


async def _do_music_search(update, context, query):
    uid = update.effective_user.id
    if not await _acquire_job(update, uid):
        return
    chat_id = update.effective_chat.id
    status = await update.message.reply_text(f"🔎 در حال جستجوی «{query}»...")
    tmpdir = tempfile.mkdtemp(prefix="mus_")
    try:
        loop = asyncio.get_running_loop()
        async with _limiter:
            await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
            got = await loop.run_in_executor(None, download_audio_by_query, query, tmpdir)
        if not got:
            await status.edit_text(
                "❌ آهنگی پیدا نشد یا حجمش بیش از ۵۰MB بود.",
                reply_markup=_back_menu_kb(),
            )
            return
        path, title, artist = got
        await _send_media_file(context, chat_id, path, _build_caption(title, artist))
        await status.edit_text(
            f"✅ {title}" + (f" — {artist}" if artist else ""),
            reply_markup=_back_menu_kb(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("خطا در جستجوی موسیقی: %s", exc)
        try:
            await status.edit_text("❌ خطای غیرمنتظره. دوباره تلاش کنید.")
        except Exception:  # noqa: BLE001
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


def admin_panel():
    c = _config
    rows = [
        [InlineKeyboardButton("📊 آمار و وضعیت", callback_data="adm:stats")],
        [
            InlineKeyboardButton(f"🔒 قفل عضویت {_onoff(c['force_join'])}",
                                 callback_data="adm:toggle:force_join"),
            InlineKeyboardButton("📢 کانال‌ها", callback_data="adm:channels"),
        ],
        [
            InlineKeyboardButton("🧩 سرویس‌ها", callback_data="adm:services"),
            InlineKeyboardButton("⚙️ محدودیت‌ها", callback_data="adm:limits"),
        ],
        [InlineKeyboardButton(f"🚧 حالت تعمیر {_onoff(c['maintenance'])}",
                              callback_data="adm:toggle:maintenance")],
        [
            InlineKeyboardButton("✉️ پیام همگانی", callback_data="adm:broadcast"),
            InlineKeyboardButton("✏️ خوش‌آمد", callback_data="adm:welcome"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def limits_panel():
    c = _config
    ul = "🟢 روشن" if c.get("user_limits") else "🔴 خاموش"
    rows = [
        [InlineKeyboardButton(f"👤 محدودیت کاربران: {ul}",
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
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="adm:home")],
    ]
    return InlineKeyboardMarkup(rows)


def services_panel():
    onoff = lambda b: "🟢" if b else "🔴"  # noqa: E731
    rows = [
        [InlineKeyboardButton(
            f"{onoff(_config['services'][k])} {SERVICE_LABELS[k]}",
            callback_data=f"adm:svc:{k}")]
        for k in ("instagram", "facebook", "youtube", "music")
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
            f"  یوتیوب {_onoff(svc['youtube'])}  موسیقی {_onoff(svc['music'])}\n"
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
    await app.bot.set_my_commands(
        [
            BotCommand("start", "شروع / منوی اصلی"),
            BotCommand("help", "راهنما"),
            BotCommand("id", "نمایش آی‌دی عددی من"),
            BotCommand("admin", "پنل مدیریت (فقط ادمین)"),
        ]
    )


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
