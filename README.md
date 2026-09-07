# Instagram & Facebook Media Bot

ربات حرفه‌ای تلگرام برای دانلود رسانه از Instagram و Facebook و یافتن نسخه کامل موسیقی ویدیو.

## گردش کار

کاربر فقط یک لینک عمومی می‌فرستد. ربات:

1. پلتفرم و اعتبار لینک را تشخیص می‌دهد.
2. بهترین کیفیت قابل ارسال در تلگرام را دانلود می‌کند.
3. ویدیو یا تصویر را برای کاربر می‌فرستد.
4. صدای ویدیو را موقتاً برای تشخیص آماده می‌کند؛ این صدای ناقص ارسال نمی‌شود.
5. آهنگ را با Shazam شناسایی می‌کند.
6. لینک‌های جست‌وجوی آهنگ را می‌فرستد. با `FULL_SONG_DOWNLOAD=true` دریافت و ارسال نسخه کامل MP3 نیز فعال می‌شود؛ در صورت شکست، لینک‌ها همچنان در دسترس‌اند.

فایل صوتی، Voice، Video و Video Note ارسالی کاربر نیز مستقیماً برای شناسایی آهنگ پذیرفته می‌شود.

## امکانات مدیریتی

- پنل مخفی ادمین با `/admin`
- عضویت اجباری در کانال‌ها
- فعال/غیرفعال‌کردن مستقل Instagram و Facebook
- محدودیت نرخ هر کاربر و سقف پردازش هم‌زمان
- حالت تعمیر، آمار، پیام همگانی و پیام خوش‌آمد سفارشی
- ذخیره تنظیمات و کاربران در `DATA_DIR`

## متغیرهای محیطی

| متغیر | الزامی | توضیح |
|---|---:|---|
| `BOT_TOKEN` | بله | توکن جدید BotFather |
| `ADMIN_IDS` | خیر | شناسه عددی ادمین‌ها، جداشده با کاما |
| `FORCE_JOIN` | خیر | فعال‌سازی عضویت اجباری |
| `REQUIRED_CHANNELS` | خیر | کانال‌ها مانند `@channel1,@channel2` |
| `DATA_DIR` | خیر | مسیر ذخیره‌سازی؛ پیش‌فرض `data` |
| `USER_LIMITS` | خیر | فعال‌سازی محدودیت کاربران |
| `RATE_LIMIT_PER_MIN` | خیر | تعداد درخواست هر کاربر در دقیقه |
| `MAX_CONCURRENT` | خیر | سقف پردازش هم‌زمان |
| `DOWNLOAD_RETRIES` | خیر | تعداد تلاش مجدد دانلود |
| `COOKIES_FILE` | خیر | مسیر فایل Cookie با فرمت Netscape |
| `INSTAGRAM_COOKIES_B64` | خیر | محتوای Base64 فایل Cookie؛ مناسب Environment امن Render |
| `PROXY_URLS` | خیر | یک یا چند Proxy جداشده با کاما برای چرخش IP |
| `BROWSER_USER_AGENT` | خیر | User-Agent همان مرورگری که Cookie از آن صادر شده |
| `WEBHOOK_URL` | خیر | در Render خودکار تنظیم می‌شود |
| `PORT` | خیر | پورت webhook؛ پیش‌فرض 10000 |
| `FULL_SONG_DOWNLOAD` | خیر | ارسال نسخه کامل MP3؛ پیش‌فرض `false` |

## اجرا

```bash
pip install -r requirements.txt
export BOT_TOKEN="your-token"
python bot.py
```

برای تبدیل صدا، `ffmpeg` باید نصب باشد. Dockerfile پروژه آن را خودکار نصب می‌کند.

## استقرار Render

سرویس با Docker و webhook اجرا می‌شود. Render به شاخه `master` متصل است و Auto-Deploy روی `On Commit` قرار دارد.

> توکن ربات را هرگز در GitHub ذخیره نکنید. فقط آن را در Environment سرویس قرار دهید.


## پایداری Instagram و خطای 429

Instagram گاهی IPهای اشتراکی دیتاسنترها مانند Render را حتی برای محتوای عمومی محدود می‌کند.
ربات روی 429 فوراً متوقف می‌شود تا مسدودیت را شدیدتر نکند و از این مسیرهای رسمی yt-dlp پشتیبانی می‌کند:

1. Cookie تازه با فرمت Netscape در `INSTAGRAM_COOKIES_B64`
2. User-Agent هماهنگ در `BROWSER_USER_AGENT`
3. Proxy سالم در `PROXY_URLS`

Cookie و Proxy محرمانه‌اند و نباید داخل مخزن Commit شوند.

## Reliability and testing

- Only HTTP(S) links on Instagram/Facebook domains are accepted; URL credentials and nonstandard ports are rejected.
- Each user can have one pending job. Up to 32 jobs may be admitted globally; the existing admin concurrency setting controls active video processing. These resource safeguards apply even when optional rate limits are disabled.
- Video/audio uploads are capped at 49 MiB. Images over 9 MiB and WebM/MKV files are sent as documents. Oversized files are rejected rather than automatically compressed.
- Music recognition uses a 45-second sample and a 60-second recognition timeout. Direct Telegram recognition uploads must be at most 20 MiB unless usable audio metadata is provided.
- `FULL_SONG_DOWNLOAD=true` enables best-match YouTube song search and MP3 conversion. Search results are not guaranteed to be the exact recording. FFmpeg is required.
- Instagram HTTP 429 stops the fallback chain to avoid additional rate-limited requests.
- Settings and user lists still require a persistent `DATA_DIR` to survive redeploys; the included free Render configuration does not provision persistent storage.

Run offline regression tests with `python -m unittest discover -s tests -v`.
GitHub Actions additionally installs dependencies and checks that the full bot module imports.
Live download, recognition, and Telegram delivery require a configured deployment and are separate integration checks.
