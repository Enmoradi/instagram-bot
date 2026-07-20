"""
ربات چندمنظوره‌ی دانلود و موسیقی برای تلگرام
-------------------------------------------------
سرویس‌ها (اول از منو انتخاب می‌شوند):
  📷 اینستاگرام | 📘 فیسبوک | ▶️ یوتیوب | 🎵 موسیقی

قابلیت‌ها:
  - دانلود ویدیو/عکس از اینستاگرام، فیسبوک و یوتیوب (با yt-dlp)
  - انتخاب خودکار بهترین کیفیت زیر ۵۰ مگابایت (سقف ربات‌های تلگرام)
  - موسیقی: جستجو با نام آهنگ  +  تشخیص آهنگ از روی کلیپ صوتی (Shazam)
  - حذف کامل فایل‌ها بلافاصله بعد از ارسال (مصرف حافظه ≈ صفر)
  - پردازش همزمان درخواست‌ها برای تعداد کاربر بالا
  - اجرا در دو حالت polling و webhook (هاست وب رایگان)
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import yt_dlp

# تشخیص آهنگ اختیاری است؛ اگر کتابخانه نصب نبود، ربات بدون این قابلیت کار می‌کند.
try:
    from shazamio import Shazam  # type: ignore

    _SHAZAM_AVAILABLE = True
except Exception:  # noqa: BLE001
    _SHAZAM_AVAILABLE = False

# ---------------------------------------------------------------------------
# پیکربندی
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

# سقف حجم آپلود ربات‌های تلگرام: ۵۰ مگابایت
MAX_TELEGRAM_BYTES = 50 * 1024 * 1024
# کیفیت‌هایی که به‌ترتیب امتحان می‌شوند تا فایل زیر ۵۰MB به‌دست آید
HEIGHT_CAPS = [1080, 720, 480, 360, 240]

# مسیر اختیاری فایل کوکی برای محتوای محدودشده (اینستاگرام/فیسبوک)
COOKIES_FILE = os.environ.get("COOKIES_FILE")

# دامنه‌ها برای تشخیص خودکار سرویس از روی لینک
PLATFORM_PATTERNS = {
    "instagram": re.compile(r"instagram\.com|instagr\.am", re.I),
    "facebook": re.compile(r"facebook\.com|fb\.watch|fb\.com", re.I),
    "youtube": re.compile(r"youtube\.com|youtu\.be", re.I),
}
URL_RE = re.compile(r"https?://\S+", re.I)

WELCOME = (
    "👋 سلام!\n\n"
    "به ربات دانلود و موسیقی خوش آمدید.\n"
    "یکی از سرویس‌های زیر را انتخاب کنید 👇"
)

MENU = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("📷 اینستاگرام", callback_data="mode:instagram"),
            InlineKeyboardButton("📘 فیسبوک", callback_data="mode:facebook"),
        ],
        [
            InlineKeyboardButton("▶️ یوتیوب", callback_data="mode:youtube"),
            InlineKeyboardButton("🎵 موسیقی", callback_data="mode:music"),
        ],
    ]
)

PROMPTS = {
    "instagram": "📷 لینک پست یا ریلز اینستاگرام را بفرستید.",
    "facebook": "📘 لینک ویدیوی فیسبوک را بفرستید.",
    "youtube": "▶️ لینک ویدیوی یوتیوب را بفرستید.",
    "music": (
        "🎵 حالت موسیقی:\n\n"
        "• نام آهنگ یا خواننده را تایپ کنید تا پیدا و ارسال شود.\n"
        "• یا یک پیام صوتی/ویدیو بفرستید تا آهنگش را تشخیص دهم."
    ),
}


# ---------------------------------------------------------------------------
# توابع کمکی (blocking) — در executor اجرا می‌شوند
# ---------------------------------------------------------------------------
def _base_ydl_opts(dest_dir: str) -> dict:
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


def download_video(url: str, dest_dir: str):
    """ویدیو/عکس را دانلود کرده و مسیر فایل را برمی‌گرداند (زیر ۵۰MB)."""
    last_path = None
    for cap in HEIGHT_CAPS:
        # پاک کردن باقی‌مانده‌ی تلاش قبلی
        for f in os.listdir(dest_dir):
            try:
                os.remove(os.path.join(dest_dir, f))
            except OSError:
                pass

        fmt = (
            f"bv*[height<={cap}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={cap}][ext=mp4]/"
            f"b[height<={cap}]/b"
        )
        opts = _base_ydl_opts(dest_dir)
        opts["format"] = fmt
        opts["merge_output_format"] = "mp4"

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("تلاش دانلود (cap=%s) ناموفق: %s", cap, exc)
            continue

        files = [
            os.path.join(dest_dir, f)
            for f in os.listdir(dest_dir)
            if f.lower().endswith((".mp4", ".mkv", ".webm", ".jpg", ".jpeg", ".png"))
        ]
        if not files:
            continue
        last_path = max(files, key=os.path.getsize)
        if os.path.getsize(last_path) <= MAX_TELEGRAM_BYTES:
            return last_path
        # عکس‌ها معمولاً کوچک‌اند؛ اگر عکس بود همان را برگردان
        if last_path.lower().endswith((".jpg", ".jpeg", ".png")):
            return last_path

    # هیچ نسخه‌ی زیر ۵۰MB پیدا نشد
    if last_path and last_path.lower().endswith((".jpg", ".jpeg", ".png")):
        return last_path
    return None


def download_audio_by_query(query: str, dest_dir: str):
    """آهنگ را بر اساس نام جستجو و به‌صورت MP3 دانلود می‌کند.

    خروجی: (مسیر فایل, عنوان, خواننده) یا None.
    """
    opts = _base_ydl_opts(dest_dir)
    opts["format"] = "bestaudio/best"
    opts["postprocessors"] = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }
    ]
    # اگر لینک نبود، در یوتیوب جستجو کن
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
    if not mp3s:
        return None
    path = mp3s[0]
    if os.path.getsize(path) > MAX_TELEGRAM_BYTES:
        return None
    title = info.get("track") or info.get("title") or "Unknown"
    artist = info.get("artist") or info.get("uploader") or ""
    return path, title, artist


async def recognize_song(audio_path: str):
    """آهنگ را از روی فایل صوتی تشخیص می‌دهد. خروجی: (عنوان, خواننده) یا None."""
    if not _SHAZAM_AVAILABLE:
        return None
    try:
        shazam = Shazam()
        out = await shazam.recognize(audio_path)
        track = out.get("track")
        if not track:
            return None
        return track.get("title", ""), track.get("subtitle", "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("تشخیص آهنگ ناموفق: %s", exc)
        return None


# ---------------------------------------------------------------------------
# ارسال فایل به تلگرام
# ---------------------------------------------------------------------------
async def _send_media_file(context, chat_id, path):
    lower = path.lower()
    if lower.endswith((".jpg", ".jpeg", ".png")):
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
        with open(path, "rb") as fh:
            await context.bot.send_photo(chat_id, photo=fh)
    elif lower.endswith(".mp3"):
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
        with open(path, "rb") as fh:
            await context.bot.send_audio(chat_id, audio=fh)
    else:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
        with open(path, "rb") as fh:
            await context.bot.send_video(chat_id, video=fh, supports_streaming=True)


# ---------------------------------------------------------------------------
# هندلرها
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("mode", None)
    await update.message.reply_text(WELCOME, reply_markup=MENU)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "برای شروع /start را بزنید و یک سرویس انتخاب کنید.\n"
        "یا مستقیم لینک اینستاگرام/فیسبوک/یوتیوب را بفرستید تا خودکار تشخیص دهم."
    )


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":", 1)[1]
    context.user_data["mode"] = mode
    await query.edit_message_text(PROMPTS.get(mode, "لینک را بفرستید."))


def detect_platform(text: str):
    for name, pat in PLATFORM_PATTERNS.items():
        if pat.search(text):
            return name
    return None


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    mode = context.user_data.get("mode")

    url_match = URL_RE.search(text)

    # حالت موسیقی ⇒ همیشه خروجی صوتی (چه نام آهنگ، چه لینک)
    if mode == "music":
        await _do_music_search(update, context, text)
        return

    # اگر لینک باشد (چه از منو، چه تشخیص خودکار) ⇒ دانلود ویدیو
    if url_match:
        await _do_video_download(update, context, url_match.group(0))
        return

    # نه لینک، نه حالت موسیقی
    if mode in ("instagram", "facebook", "youtube"):
        await update.message.reply_text("❌ لطفاً یک لینک معتبر بفرستید.")
    else:
        await update.message.reply_text(
            "لطفاً اول با /start یک سرویس انتخاب کنید یا یک لینک بفرستید.",
            reply_markup=MENU,
        )


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پیام صوتی/ویدیو ⇒ تشخیص آهنگ و ارسال MP3."""
    chat_id = update.effective_chat.id
    if not _SHAZAM_AVAILABLE:
        await update.message.reply_text(
            "❌ قابلیت تشخیص آهنگ روی این سرور فعال نیست."
        )
        return

    status = await update.message.reply_text("🎧 در حال تشخیص آهنگ...")
    tmpdir = tempfile.mkdtemp(prefix="rec_")
    try:
        tg_file = None
        msg = update.message
        if msg.voice:
            tg_file = await msg.voice.get_file()
        elif msg.audio:
            tg_file = await msg.audio.get_file()
        elif msg.video:
            tg_file = await msg.video.get_file()
        elif msg.video_note:
            tg_file = await msg.video_note.get_file()

        if tg_file is None:
            await status.edit_text("❌ فایل صوتی پیدا نشد.")
            return

        src = os.path.join(tmpdir, "input")
        await tg_file.download_to_drive(src)

        result = await recognize_song(src)
        if not result or not result[0]:
            await status.edit_text("❌ نتوانستم آهنگ را تشخیص دهم. کلیپ واضح‌تری بفرستید.")
            return

        title, artist = result
        await status.edit_text(f"🎵 پیدا شد:\n*{title}* — {artist}\n\n⏳ در حال ارسال...",
                               parse_mode="Markdown")

        loop = asyncio.get_running_loop()
        got = await loop.run_in_executor(
            None, download_audio_by_query, f"{title} {artist}", tmpdir
        )
        if got:
            path, _t, _a = got
            await _send_media_file(context, chat_id, path)
            await status.edit_text(f"✅ {title} — {artist}")
        else:
            await status.edit_text(
                f"🎵 آهنگ شناسایی شد:\n*{title}* — {artist}\n"
                "ولی فایلش پیدا نشد.",
                parse_mode="Markdown",
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("خطا در تشخیص: %s", exc)
        try:
            await status.edit_text("❌ خطا در پردازش. دوباره تلاش کنید.")
        except Exception:  # noqa: BLE001
            pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# منطق‌های مشترک
# ---------------------------------------------------------------------------
async def _do_video_download(update, context, url):
    chat_id = update.effective_chat.id
    status = await update.message.reply_text("⏳ در حال دانلود... کمی صبر کنید.")
    tmpdir = tempfile.mkdtemp(prefix="dl_")
    try:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
        loop = asyncio.get_running_loop()
        path = await loop.run_in_executor(None, download_video, url, tmpdir)
        if not path:
            await status.edit_text(
                "❌ دانلود ناموفق بود.\n"
                "شاید محتوا خصوصی باشد، یا حتی کم‌کیفیت‌ترین نسخه هم از ۵۰MB بزرگ‌تر است."
            )
            return
        await _send_media_file(context, chat_id, path)
        await status.edit_text("✅ انجام شد!")
    except Exception as exc:  # noqa: BLE001
        logger.exception("خطا در دانلود ویدیو: %s", exc)
        try:
            await status.edit_text("❌ خطای غیرمنتظره. دوباره تلاش کنید.")
        except Exception:  # noqa: BLE001
            pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        logger.info("پوشه‌ی موقت حذف شد: %s", tmpdir)


async def _do_music_search(update, context, query):
    chat_id = update.effective_chat.id
    status = await update.message.reply_text(f"🔎 در حال جستجوی «{query}»...")
    tmpdir = tempfile.mkdtemp(prefix="mus_")
    try:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
        loop = asyncio.get_running_loop()
        got = await loop.run_in_executor(
            None, download_audio_by_query, query, tmpdir
        )
        if not got:
            await status.edit_text("❌ آهنگی پیدا نشد یا حجمش بیش از ۵۰MB بود.")
            return
        path, title, artist = got
        await _send_media_file(context, chat_id, path)
        await status.edit_text(f"✅ {title}" + (f" — {artist}" if artist else ""))
    except Exception as exc:  # noqa: BLE001
        logger.exception("خطا در جستجوی موسیقی: %s", exc)
        try:
            await status.edit_text("❌ خطای غیرمنتظره. دوباره تلاش کنید.")
        except Exception:  # noqa: BLE001
            pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("خطا هنگام پردازش آپدیت:", exc_info=context.error)


# ---------------------------------------------------------------------------
# اجرا
# ---------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("متغیر محیطی BOT_TOKEN تنظیم نشده است.")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)  # پردازش همزمان برای کاربران زیاد
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^mode:"))
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
