
import instaloader
import os
import tempfile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telegram import Update

BOT_TOKEN = "7488688385:AAGbIEU7mf_96Lr9JwWEU216VX2aeRFFa-o"

L = instaloader.Instaloader()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("سلام! لینک پست یا ریلز اینستاگرام را بفرستید تا دانلود کنم.")

def download_instagram_post(url):
    with tempfile.TemporaryDirectory() as tmpdirname:
        # دانلود پست یا ریلز داخل پوشه موقت
        try:
            L.download_post(instaloader.Post.from_shortcode(L.context, extract_shortcode(url)), tmpdirname)
        except Exception as e:
            print("خطا در دانلود:", e)
            return None

        # فایل‌ها را پیدا کن (عکس یا ویدیو)
        files = []
        for root, dirs, filenames in os.walk(tmpdirname):
            for filename in filenames:
                if filename.endswith((".jpg", ".mp4")):
                    files.append(os.path.join(root, filename))

        if not files:
            return None
        return files

def extract_shortcode(url):
    # گرفتن کد پست از URL
    import re
    pattern = r"instagram\.com\/(p|reel|tv)\/([a-zA-Z0-9_-]+)"
    match = re.search(pattern, url)
    if match:
        return match.group(2)
    return None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    shortcode = extract_shortcode(url)
    if not shortcode:
        await update.message.reply_text("لینک اینستاگرام معتبر نیست. لطفا فقط لینک پست یا ریلز بفرستید.")
        return

    await update.message.reply_text("در حال دانلود پست... لطفا کمی صبر کنید.")

    files = download_instagram_post(url)
    if not files:
        await update.message.reply_text("نتوانستم پست را دانلود کنم. مطمئن شوید لینک پابلیک است.")
        return

    for file in files:
        if file.endswith(".mp4"):
            await context.bot.send_video(update.effective_chat.id, video=open(file, "rb"))
        elif file.endswith(".jpg"):
            await context.bot.send_photo(update.effective_chat.id, photo=open(file, "rb"))

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("ربات اجرا شد")
    await app.run_polling()

if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    import asyncio
    asyncio.run(main())
