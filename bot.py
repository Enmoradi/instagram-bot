"""
ربات دانلود اینستاگرام برای تلگرام
- دانلود پست، ریلز، عکس و ویدیو
- پشتیبانی از پست‌های چندتایی (کاروسل)
- حذف خودکار فایل‌ها بعد از ارسال (مصرف حافظه صفر)
- اجرا در دو حالت polling (لوکال/ورکر) و webhook (هاست وب رایگان)
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile

import instaloader
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# پیکربندی
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("insta-bot")
# جلوگیری از لاگ‌های اضافه‌ی httpx
logging.getLogger("httpx").setLevel(logging.WARNING)

# توکن ربات از متغیر محیطی خوانده می‌شود. اگر تنظیم نشده باشد از مقدار پیش‌فرض
# استفاده می‌شود تا ربات همچنان کار کند (توصیه: BOT_TOKEN را در هاست تنظیم کنید).
_DEFAULT_TOKEN = "7488688385:AAGbIEU7mf_96Lr9JwWEU216VX2aeRFFa-o"
BOT_TOKEN = os.environ.get("BOT_TOKEN", _DEFAULT_TOKEN)

# اگر WEBHOOK_URL تنظیم شده باشد، ربات در حالت webhook اجرا می‌شود؛ در غیر این
# صورت در حالت polling. برای هاست‌های وب رایگان (مثل Render) از webhook استفاده کنید.
# Render به‌طور خودکار RENDER_EXTERNAL_URL را تنظیم می‌کند (پیکربندی صفر).
WEBHOOK_URL = (
    os.environ.get("WEBHOOK_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or ""
).rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))

# محدودیت حجم آپلود برای ربات‌های تلگرام: ۵۰ مگابایت
MAX_TELEGRAM_BYTES = 50 * 1024 * 1024

# اطلاعات ورود اختیاری اینستاگرام برای کاهش خطای محدودیت (rate-limit).
IG_USERNAME = os.environ.get("IG_USERNAME")
IG_PASSWORD = os.environ.get("IG_PASSWORD")

# الگوی استخراج کد کوتاه (shortcode) از لینک‌های اینستاگرام
SHORTCODE_RE = re.compile(
    r"(?:instagram\.com|instagr\.am)/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# نمونه‌ی Instaloader (یک بار ساخته می‌شود)
# ---------------------------------------------------------------------------
L = instaloader.Instaloader(
    dirname_pattern="{target}",
    save_metadata=False,
    download_comments=False,
    download_geotags=False,
    compress_json=False,
    post_metadata_txt_pattern="",
    quiet=True,
)


def _try_login() -> None:
    """ورود اختیاری به اینستاگرام برای کاهش محدودیت‌ها."""
    if not (IG_USERNAME and IG_PASSWORD):
        return
    session_path = f"/tmp/ig_session_{IG_USERNAME}"
    try:
        L.load_session_from_file(IG_USERNAME, session_path)
        logger.info("نشست اینستاگرام از فایل بارگذاری شد.")
        return
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("بارگذاری نشست ناموفق بود: %s", exc)
    try:
        L.login(IG_USERNAME, IG_PASSWORD)
        L.save_session_to_file(session_path)
        logger.info("ورود به اینستاگرام موفق بود.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("ورود به اینستاگرام ناموفق بود (ادامه بدون ورود): %s", exc)


# ---------------------------------------------------------------------------
# منطق دانلود
# ---------------------------------------------------------------------------
def extract_shortcode(text: str):
    match = SHORTCODE_RE.search(text)
    return match.group(1) if match else None


def download_post(shortcode: str, dest_dir: str):
    """پست را در dest_dir دانلود کرده و لیست فایل‌های مدیا را برمی‌گرداند.

    این تابع همگام (blocking) است و باید در executor اجرا شود.
    """
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    L.download_post(post, target=dest_dir)

    media = []
    for root, _dirs, filenames in os.walk(dest_dir):
        for name in sorted(filenames):
            if name.lower().endswith((".mp4", ".jpg", ".jpeg", ".png")):
                media.append(os.path.join(root, name))
    return media


# ---------------------------------------------------------------------------
# هندلرهای تلگرام
# ---------------------------------------------------------------------------
WELCOME = (
    "👋 سلام!\n\n"
    "من یک ربات دانلود اینستاگرام هستم.\n"
    "کافیست لینک یک *پست*، *ریلز* یا *IGTV* را برایم بفرستید تا برایتان دانلود کنم.\n\n"
    "⚠️ توجه: فقط پست‌های عمومی (public) قابل دانلود هستند."
)

HELP = (
    "📖 راهنما:\n\n"
    "۱. لینک پست یا ریلز اینستاگرام را کپی کنید.\n"
    "۲. لینک را برای من ارسال کنید.\n"
    "۳. چند لحظه صبر کنید تا فایل را برایتان بفرستم.\n\n"
    "نمونه لینک:\n"
    "`https://www.instagram.com/reel/XXXXXXXXX/`"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    shortcode = extract_shortcode(text)

    if not shortcode:
        await update.message.reply_text(
            "❌ لینک اینستاگرام معتبر نیست.\n"
            "لطفاً فقط لینک یک پست، ریلز یا IGTV بفرستید."
        )
        return

    status = await update.message.reply_text("⏳ در حال دانلود... کمی صبر کنید.")
    chat_id = update.effective_chat.id

    # هر درخواست در یک پوشه‌ی موقت جدا دانلود می‌شود و در پایان کاملاً حذف می‌گردد.
    tmpdir = tempfile.mkdtemp(prefix="ig_")
    try:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)

        loop = asyncio.get_running_loop()
        try:
            media = await loop.run_in_executor(
                None, download_post, shortcode, tmpdir
            )
        except instaloader.exceptions.InstaloaderException as exc:
            logger.warning("خطای اینستالودر (%s): %s", shortcode, exc)
            await status.edit_text(
                "❌ نتوانستم این پست را دانلود کنم.\n"
                "ممکن است خصوصی (private) باشد یا حذف شده باشد."
            )
            return

        if not media:
            await status.edit_text("❌ هیچ فایل قابل دانلودی در این پست پیدا نشد.")
            return

        sent = 0
        for path in media:
            size = os.path.getsize(path)
            if size > MAX_TELEGRAM_BYTES:
                await context.bot.send_message(
                    chat_id,
                    f"⚠️ یک فایل به دلیل حجم زیاد (بیش از ۵۰ مگابایت) ارسال نشد.",
                )
                continue

            try:
                if path.lower().endswith(".mp4"):
                    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
                    with open(path, "rb") as fh:
                        await context.bot.send_video(chat_id, video=fh)
                else:
                    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
                    with open(path, "rb") as fh:
                        await context.bot.send_photo(chat_id, photo=fh)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.error("خطا در ارسال فایل %s: %s", path, exc)

        if sent:
            await status.edit_text(f"✅ انجام شد! ({sent} فایل ارسال شد)")
        else:
            await status.edit_text("❌ ارسال فایل‌ها ناموفق بود.")

    except Exception as exc:  # noqa: BLE001
        logger.exception("خطای غیرمنتظره: %s", exc)
        try:
            await status.edit_text("❌ خطای غیرمنتظره‌ای رخ داد. دوباره تلاش کنید.")
        except Exception:  # noqa: BLE001
            pass
    finally:
        # حذف کامل تمام فایل‌های دانلودشده تا حافظه‌ی هاست پر نشود.
        shutil.rmtree(tmpdir, ignore_errors=True)
        logger.info("پوشه‌ی موقت حذف شد: %s", tmpdir)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("خطا هنگام پردازش آپدیت:", exc_info=context.error)


# ---------------------------------------------------------------------------
# اجرا
# ---------------------------------------------------------------------------
def main():
    if not BOT_TOKEN or BOT_TOKEN == _DEFAULT_TOKEN:
        logger.warning(
            "از توکن پیش‌فرض استفاده می‌شود! برای امنیت، متغیر محیطی BOT_TOKEN را "
            "تنظیم کرده و توکن را از BotFather دوباره بسازید."
        )

    _try_login()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
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
