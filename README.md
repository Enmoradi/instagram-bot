# Music Search, Recognition & Media Bot

ربات تلگرام برای جستجوی آهنگ، تشخیص موسیقی از وویس و ویدیو، دریافت MP3 و دانلود رسانه از Instagram و Facebook.

## تجربهٔ کاربری موسیقی

- نام آهنگ، خواننده یا بخشی از شعر را فارسی یا انگلیسی بفرستید؛ تا پنج نتیجهٔ جستجو نمایش داده می‌شود.
- نتیجه را انتخاب کنید تا همان نسخه به MP3 تبدیل و ارسال شود؛ دانلود تعاملی به `FULL_SONG_DOWNLOAD` وابسته نیست.
- وویس، صوت، ویدیو یا فایل صوتی/ویدیویی بفرستید؛ پس از تشخیص، دکمهٔ «انتخاب و دانلود MP3» نتایج آهنگ را نشان می‌دهد.
- با دکمهٔ قلب آهنگ را ذخیره کنید. `/favorites` علاقه‌مندی‌ها و `/recent` آهنگ‌های اخیر را در گفت‌وگوی خصوصی نشان می‌دهند.
- `/forget` تاریخچهٔ موسیقی و علاقه‌مندی‌های شما را حذف می‌کند. آمار کاربران مدیریتی جداست.
- جستجوی Inline با نوشتن `@YourBotUsername نام آهنگ` کار می‌کند. آهنگ‌های ذخیره‌شده مستقیم به‌صورت صوت ارسال می‌شوند؛ نتایج تازه کارت دریافت در ربات دارند.
- برای فعال‌سازی Inline، در BotFather دستور `/setinline` را برای ربات خود تنظیم کنید. وقتی عضویت اجباری روشن است، نتایج Inline غیرفعال‌اند؛ جستجوی خصوصی پس از بررسی عضویت کار می‌کند.

جستجو از YouTube استفاده می‌کند؛ جستجو با بخشی از شعر تضمین تطبیق دقیق ندارد. دکمهٔ «متن ترانه» نسخه‌های موجود در LRCLIB را نشان می‌دهد و متن نسخهٔ انتخابی را داخل ربات می‌فرستد. متن‌های طولانی به‌صورت فایل ارائه می‌شوند. جستجوی Genius نیز به‌عنوان گزینهٔ بیرونی موجود است. این پروژه به کاتالوگ خصوصی WhatsMusic متصل نیست و تشخیص زمزمه یا آواز زنده تضمین نمی‌شود.

## سلامت سرویس و زنده‌ماندن

مسیر عمومی `/healthz` پس از آماده‌شدن ربات پاسخ JSON با `status=ok` می‌دهد. وب‌هوک `/telegram` فقط درخواست دارای هدر محرمانهٔ Telegram را قبول می‌کند؛ توکن ربات دیگر داخل URL نیست. هنگام راه‌اندازی مجدد، پیام‌های منتظر حذف نمی‌شوند.

GitHub Actions هر پنج دقیقه `/healthz` را بررسی می‌کند و پاسخ خطا را شکست گزارش می‌کند. این زمان‌بندی روی شاخهٔ پیش‌فرض فعال است. زمان‌بند GitHub ممکن است تأخیر داشته باشد، بنابراین این روش روی پلن رایگان **تضمین روشن‌ماندن دائمی نیست**. برای حذف خواب ناشی از بی‌فعالیتی، پلن پولی Render لازم است. مخزن به‌تنهایی هیچ پلن پولی را فعال نمی‌کند.

پلن رایگان Render سهمیهٔ ساعت مشترک دارد؛ چند سرویس هم‌زمان آن را زودتر مصرف می‌کنند. اطلاعات SQLite روی دیسک موقت با راه‌اندازی مجدد ممکن است از بین برود؛ برای نگه‌داری دائمی، دیسک پایدار یا دیتابیس خارجی لازم است.

## اجرای نسخهٔ جدید

Docker شامل Python، FFmpeg و Node 22 برای نیازهای فعلی yt-dlp است. برای اجرای مستقیم علاوه بر وابستگی‌های Python، FFmpeg و Node 22 یا جدیدتر را نصب کنید.

متغیرهای اختیاری موسیقی:

| متغیر | کاربرد |
|---|---|
| `MUSIC_COOKIES_FILE` | کوکی Netscape برای منبع جستجو/دانلود موسیقی؛ مستقل از کوکی Instagram |
| `MUSIC_PROXY_URL` | پراکسی اختیاری جستجو و دانلود موسیقی |

فایل `music_<bot-id>.sqlite3` در `DATA_DIR` آهنگ‌های اخیر، علاقه‌مندی‌ها و شناسهٔ فایل تلگرام را نگه می‌دارد. فایل MP3 پس از ارسال حذف می‌شود. تا ۲۰ آهنگ اخیر و ۱۰۰ علاقه‌مندی برای هر کاربر نگه‌داری می‌شود؛ فهرست‌ها صفحه‌بندی دارند. دیسک پایدار برای حفظ این اطلاعات پس از استقرار مجدد لازم است.

جستجو پس از ۲۵ ثانیه و دانلود تعاملی پس از ۱۸۰ ثانیه متوقف می‌شود. در کانتینر Linux پردازش‌های فرزند هم متوقف می‌شوند. آهنگ زنده، بیش از ۱۵ دقیقه یا فایل بیش از ۴۹ MiB پذیرفته نمی‌شود. دسترسی به منبع ممکن است به IP، کوکی و محدودیت‌های سرویس وابسته باشد.

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

Install `requirements.txt`, then run offline regression tests with `python -m unittest discover -s tests -v`.
GitHub Actions additionally installs dependencies and checks that the full bot module imports.
Live download, recognition, and Telegram delivery require a configured deployment and are separate integration checks.
