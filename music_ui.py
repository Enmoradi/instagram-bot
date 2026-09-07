"""Music-first Telegram flows, with bounded subprocesses and reusable audio."""
import asyncio
from collections import OrderedDict
import json
import io
import logging
import os
import signal
import sys
import tempfile
import time
from urllib.parse import quote_plus

from telegram import (InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard,
                      InlineQueryResultArticle, InlineQueryResultCachedAudio, InputTextMessageContent)
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, InlineQueryHandler

from music_store import MusicStore
from lyrics import fetch_lyrics

log = logging.getLogger(__name__)


async def run_worker(action, value, directory=None):
    args = [sys.executable, '-m', 'music_worker', action, value]
    if directory:
        args.append(directory)
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=(os.name != 'nt'),
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), 25 if action == 'search' else 180)
    except BaseException:
        if process.returncode is None:
            try:
                if os.name != 'nt':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
        raise
    if process.returncode:
        raise RuntimeError('Music provider unavailable')
    return json.loads(out)


class MusicUI:
    def __init__(self, data_dir, bot_id, config, acquire, release, limiter, membership, admin):
        self.store = MusicStore(os.path.join(data_dir, f'music_{bot_id}.sqlite3'))
        self.config = config
        self.acquire, self.release, self.limiter = acquire, release, limiter
        self.membership, self.admin = membership, admin
        self.search_cache = OrderedDict()
        self.inline_active = set()
        self.worker_slots = asyncio.Semaphore(3)

    def register(self, app):
        app.add_handler(CommandHandler('search', self.search_command))
        app.add_handler(CommandHandler('favorites', self.favorites))
        app.add_handler(CommandHandler('recent', self.recent))
        app.add_handler(CommandHandler('forget', self.forget))
        app.add_handler(CallbackQueryHandler(self.callback, pattern=r'^music:'))
        app.add_handler(InlineQueryHandler(self.inline))

    async def allowed(self, update, context):
        if self.config().get('maintenance') and not self.admin(update.effective_user.id):
            if update.callback_query:
                await update.callback_query.answer('🚧 ربات در حال تعمیر است.', show_alert=True)
            else:
                await update.effective_message.reply_text('🚧 ربات در حال تعمیر است.')
            return False
        return await self.membership(update, context, update.effective_user.id)

    async def worker(self, *args):
        # A timeout while waiting for a slot prevents an unbounded subprocess queue.
        await asyncio.wait_for(self.worker_slots.acquire(), 3)
        try:
            return await run_worker(*args)
        finally:
            self.worker_slots.release()

    async def search_tracks(self, query):
        query = ' '.join(query.split())[:200]
        cached = self.search_cache.get(query)
        if cached and time.monotonic() - cached[0] < 300:
            self.search_cache.move_to_end(query)
            return cached[1]
        entries = await self.worker('search', query)
        keys = [self.store.put(x['title'], x['artist'], x['id'], x['duration']) for x in entries]
        self.search_cache[query] = (time.monotonic(), keys)
        self.search_cache.move_to_end(query)
        while len(self.search_cache) > 128:
            self.search_cache.popitem(last=False)
        return keys

    def results_keyboard(self, tracks):
        rows = []
        for track in tracks:
            duration = track['duration']
            suffix = f" · {duration // 60}:{duration % 60:02}" if duration else ''
            rows.append([Button('🎵 ' + track['title'][:55] + suffix,
                                callback_data='music:dl:' + track['id'])])
        rows.append([Button('🔎 جستجوی جدید', callback_data='music:search')])
        return Keyboard(rows)

    def recognition_keyboard(self, title, artist=''):
        key = self.store.put(title, artist)
        return Keyboard([
            [Button('🎵 انتخاب و دانلود MP3', callback_data='music:dl:' + key)],
            [Button('📝 متن ترانه', callback_data='music:lyrics:' + key)],
            [Button('🔎 جستجوی متن ترانه', url='https://genius.com/search?q=' + quote_plus(title + ' ' + artist))],
            [Button('🏠 منوی اصلی', callback_data='menu')],
        ])

    def track_keyboard(self, track):
        return Keyboard([
            [Button('❤️ افزودن / حذف علاقه‌مندی', callback_data='music:favorite:' + track['id'])],
            [Button('📝 متن ترانه', callback_data='music:lyrics:' + track['id'])],
            [Button('🔎 جستجوی متن ترانه', url='https://genius.com/search?q=' + quote_plus(track['title'] + ' ' + track['artist']))],
            [Button('🔎 آهنگ دیگر', callback_data='music:search')],
        ])

    async def search_command(self, update, context):
        await self.search(update, context, ' '.join(context.args))

    async def search(self, update, context, query):
        if not await self.allowed(update, context):
            return
        if len(query.strip()) < 2:
            await update.effective_message.reply_text('🔎 نام آهنگ، خواننده یا بخشی از شعر را بنویسید.')
            return
        uid = update.effective_user.id
        if not await self.acquire(update, uid):
            return
        status = None
        try:
            status = await update.effective_message.reply_text('🔎 در حال جستجوی آهنگ…')
            async with self.limiter:
                keys = await self.search_tracks(query)
            tracks = [self.store.get(key) for key in keys]
            tracks = [track for track in tracks if track]
            await status.edit_text(
                '🎧 یکی از نتایج را برای دریافت MP3 انتخاب کنید:' if tracks else
                'آهنگی پیدا نشد؛ نام خواننده را اضافه کنید یا عبارت دیگری بفرستید.',
                reply_markup=self.results_keyboard(tracks),
            )
        except Exception:
            log.warning('Music search failed', exc_info=True)
            if status:
                await status.edit_text('جستجو فعلاً در دسترس نیست؛ کمی بعد دوباره تلاش کنید.')
        finally:
            self.release(uid)

    async def download(self, update, context, key):
        track = self.store.get(key)
        if not track:
            await update.effective_message.reply_text('این نتیجه قدیمی است؛ دوباره نام آهنگ را جستجو کنید.')
            return
        if not track['source_id']:
            await self.search(update, context, track['title'] + ' ' + track['artist'])
            return
        if not await self.allowed(update, context):
            return
        uid = update.effective_user.id
        if not await self.acquire(update, uid):
            return
        status = None
        try:
            status = await update.effective_message.reply_text('🎧 در حال آماده‌سازی MP3…')
            sent = None
            async with self.limiter:
                kwargs = dict(chat_id=update.effective_chat.id,
                              title=track['title'], performer=track['artist'] or None,
                              reply_markup=self.track_keyboard(track))
                if track['file_id']:
                    try:
                        sent = await context.bot.send_audio(audio=track['file_id'], **kwargs)
                    except BadRequest:
                        self.store.cache(key, None)
                if sent is None:
                    with tempfile.TemporaryDirectory(prefix='music_') as directory:
                        await self.worker('download', track['source_id'], directory)
                        with open(os.path.join(directory, 'music.mp3'), 'rb') as audio:
                            sent = await context.bot.send_audio(audio=audio, write_timeout=120, **kwargs)
                if sent.audio:
                    self.store.cache(key, sent.audio.file_id)
                self.store.remember(uid, key)
            await status.edit_text('✅ آهنگ ارسال شد. نام آهنگ بعدی یا یک وویس بفرستید.')
        except Exception:
            log.warning('Music delivery failed', exc_info=True)
            if status:
                await status.edit_text('دریافت این نسخه ممکن نشد؛ یک نتیجهٔ دیگر انتخاب کنید. آهنگ باید کمتر از ۱۵ دقیقه باشد.')
        finally:
            self.release(uid)

    async def callback(self, update, context):
        if not await self.allowed(update, context):
            return
        query = update.callback_query
        parts = query.data.split(':', 2)
        action = parts[1]
        await query.answer()
        if action == 'search':
            context.user_data.pop('mode', None)
            await update.effective_message.reply_text('🔎 نام آهنگ، خواننده یا بخشی از شعر را بنویسید؛ برای شناسایی، وویس یا ویدیو بفرستید.')
        elif action == 'favorites':
            await self.favorites(update, context)
        elif action == 'recent':
            await self.recent(update, context)
        elif len(parts) == 3 and action in ('favpage', 'recentpage') and parts[2].isdigit():
            await self.library(update, context, action == 'favpage', min(90, int(parts[2])))
        elif len(parts) == 3 and action == 'dl':
            await self.download(update, context, parts[2])
        elif len(parts) == 3 and action in ('lyrics', 'lrc'):
            await self.lyrics(update, context, action, parts[2])
        elif len(parts) == 3 and action == 'favorite':
            if not self.store.get(parts[2]):
                await update.effective_message.reply_text('این نتیجه قدیمی است؛ دوباره جستجو کنید.')
                return
            try:
                enabled = self.store.toggle_favorite(update.effective_user.id, parts[2])
                await update.effective_message.reply_text('❤️ به علاقه‌مندی‌ها اضافه شد.' if enabled else 'از علاقه‌مندی‌ها حذف شد.')
            except ValueError:
                await update.effective_message.reply_text('حداکثر ۱۰۰ علاقه‌مندی؛ ابتدا یک آهنگ را حذف کنید.')

    async def lyrics(self, update, context, action, key):
        uid = update.effective_user.id
        if not await self.acquire(update, uid):
            return
        try:
            if action == 'lyrics':
                track = self.store.get(key)
                if not track:
                    await update.effective_message.reply_text('نتیجه قدیمی است؛ دوباره جستجو کنید.')
                    return
                entries = await fetch_lyrics('search', {'q': track['title']})
                rows = []
                for entry in entries:
                    if isinstance(entry.get('id'), int) and entry.get('plainLyrics'):
                        label = (entry.get('trackName', '') + ' — ' + entry.get('artistName', ''))[:70]
                        rows.append([Button(label, callback_data='music:lrc:' + str(entry['id']))])
                    if len(rows) == 5:
                        break
                await update.effective_message.reply_text(
                    '📝 نسخهٔ متن ترانه را انتخاب کنید (منبع: LRCLIB):' if rows else 'متن این آهنگ در منبع پیدا نشد.',
                    reply_markup=Keyboard(rows) if rows else None)
            elif key.isdigit():
                entry = await fetch_lyrics('get/' + key)
                content = entry.get('plainLyrics') or ''
                if not content:
                    await update.effective_message.reply_text('متن این نسخه در دسترس نیست.')
                    return
                heading = ('📝 ' + entry.get('trackName', '') + ' — ' + entry.get('artistName', '') + '\nمنبع: LRCLIB\n\n')[:400]
                if len((heading + content).encode('utf-16-le')) // 2 <= 3900:
                    await update.effective_message.reply_text(heading + content)
                else:
                    await context.bot.send_document(update.effective_chat.id,
                        document=io.BytesIO(content.encode('utf-8')), filename='lyrics.txt', caption=heading)
        except Exception:
            log.warning('Lyrics lookup failed', exc_info=True)
            await update.effective_message.reply_text('سرویس متن ترانه فعلاً پاسخ نداد؛ کمی بعد دوباره تلاش کنید.')
        finally:
            self.release(uid)

    async def library(self, update, context, favorites, offset=0):
        if not await self.allowed(update, context):
            return
        if update.effective_chat.type != 'private':
            await update.effective_message.reply_text('برای دیدن فهرست شخصی، در گفت‌وگوی خصوصی ربات این دستور را بفرستید.')
            return
        tracks = self.store.listing(update.effective_user.id, favorites, offset)
        keyboard = [list(row) for row in self.results_keyboard(tracks[:10]).inline_keyboard]
        prefix = 'music:favpage:' if favorites else 'music:recentpage:'
        navigation = []
        if offset:
            navigation.append(Button('⬅️ قبلی', callback_data=prefix + str(max(0, offset - 10))))
        if len(tracks) > 10:
            navigation.append(Button('بعدی ➡️', callback_data=prefix + str(offset + 10)))
        if navigation:
            keyboard.append(navigation)
        await update.effective_message.reply_text(
            ('❤️ علاقه‌مندی‌ها' if favorites else '🕘 آهنگ‌های اخیر') if tracks else 'هنوز آهنگی در این فهرست نیست.',
            reply_markup=Keyboard(keyboard))

    async def favorites(self, update, context):
        await self.library(update, context, True)

    async def recent(self, update, context):
        await self.library(update, context, False)

    async def forget(self, update, context):
        self.store.forget(update.effective_user.id)
        await update.effective_message.reply_text('تاریخچهٔ آهنگ‌ها و علاقه‌مندی‌های شما پاک شد.')

    async def start_payload(self, update, context):
        if context.args and context.args[0].startswith('song_'):
            await self.download(update, context, context.args[0][5:])
            return True
        return False

    async def inline(self, update, context):
        inline = update.inline_query
        uid = inline.from_user.id
        query = inline.query.strip()
        if (len(query) < 3 or self.config().get('maintenance') or
                uid in self.inline_active or len(self.inline_active) >= 2):
            await inline.answer([], cache_time=1, is_personal=True)
            return
        self.inline_active.add(uid)
        try:
            if self.config().get('force_join') and not self.admin(uid):
                # Inline queries cannot display the normal membership join flow.
                await inline.answer([], cache_time=1, is_personal=True)
                return
            keys = await self.search_tracks(query)
            results = []
            for key in keys:
                track = self.store.get(key)
                if not track:
                    continue
                if track['file_id']:
                    results.append(InlineQueryResultCachedAudio(id=key, audio_file_id=track['file_id']))
                    continue
                url = f'https://t.me/{context.bot.username}?start=song_{key}'
                results.append(InlineQueryResultArticle(
                    id=key, title=track['title'], description=track['artist'],
                    input_message_content=InputTextMessageContent('🎵 ' + track['title'] + '\n' + track['artist'] + '\n' + url),
                    reply_markup=Keyboard([[Button('🎧 دریافت MP3 در ربات', url=url)]])))
            await inline.answer(results, cache_time=30, is_personal=True)
        except Exception:
            log.warning('Inline music search failed', exc_info=True)
            await inline.answer([], cache_time=1, is_personal=True)
        finally:
            self.inline_active.discard(uid)
