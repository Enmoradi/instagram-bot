import asyncio
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from music_store import MusicStore
from music_ui import MusicUI, run_worker


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'music.sqlite3')
        self.store = MusicStore(self.path)

    def test_cache_survives_restart_and_repeated_search(self):
        key = self.store.put('آهنگ', 'خواننده', 'abcdefghijk', 120)
        self.store.cache(key, 'telegram-file')
        self.assertEqual(self.store.put('changed title', '', 'abcdefghijk'), key)
        self.assertEqual(MusicStore(self.path).get(key)['file_id'], 'telegram-file')

    def test_favorites_are_isolated_and_toggle(self):
        key = self.store.put('Song', source_id='abcdefghijk')
        self.assertTrue(self.store.toggle_favorite(1, key))
        self.assertEqual(len(self.store.listing(1, True)), 1)
        self.assertEqual(self.store.listing(2, True), [])
        self.assertFalse(self.store.toggle_favorite(1, key))
        self.assertEqual(self.store.listing(1, True), [])

    def test_history_bounded_favorites_preserved_and_paginated(self):
        favorite = self.store.put('Keep', source_id='favorite-id')
        self.store.toggle_favorite(1, favorite)
        for i in range(30):
            key = self.store.put(str(i), source_id=str(i))
            self.store.remember(1, key)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM library WHERE user_id=1').fetchone()[0], 21)
        self.assertEqual(self.store.listing(1, True)[0]['id'], favorite)
        page1 = self.store.listing(1)
        page2 = self.store.listing(1, offset=10)
        self.assertEqual(page1[10]['id'], page2[0]['id'])

    def test_forget_does_not_delete_other_users(self):
        key = self.store.put('Song')
        self.store.remember(1, key)
        self.store.remember(2, key)
        self.store.forget(1)
        self.assertEqual(self.store.listing(1), [])
        self.assertEqual(len(self.store.listing(2)), 1)


class UITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.busy = set()

        async def acquire(update, uid):
            if uid in self.busy:
                return False
            self.busy.add(uid)
            return True

        self.ui = MusicUI(self.tmp.name, '123', lambda: {}, acquire, self.busy.discard,
                          asyncio.Semaphore(2), AsyncMock(return_value=True), lambda uid: False)
        self.message = SimpleNamespace(reply_text=AsyncMock())
        self.update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
            effective_message=self.message, effective_chat=SimpleNamespace(id=1, type='private'), callback_query=None)
        self.context = SimpleNamespace(bot=SimpleNamespace(send_audio=AsyncMock(), username='testbot'))

    async def test_search_cache_avoids_second_provider_call(self):
        self.ui.worker = AsyncMock(return_value=[{'id':'abcdefghijk','title':'Song','artist':'Artist','duration':120}])
        first = await self.ui.search_tracks('  song   artist ')
        second = await self.ui.search_tracks('song artist')
        self.assertEqual(first, second)
        self.ui.worker.assert_awaited_once()

    async def test_search_releases_slot_if_status_fails(self):
        self.message.reply_text.side_effect = RuntimeError('network')
        await self.ui.search(self.update, self.context, 'Song name')
        self.assertEqual(self.busy, set())

    async def test_cached_audio_skips_download_and_records_history(self):
        key = self.ui.store.put('Song', source_id='abcdefghijk')
        self.ui.store.cache(key, 'cached-file')
        self.ui.worker = AsyncMock()
        self.context.bot.send_audio.return_value = SimpleNamespace(audio=SimpleNamespace(file_id='cached-file'))
        await self.ui.download(self.update, self.context, key)
        self.ui.worker.assert_not_called()
        self.assertEqual(self.context.bot.send_audio.call_args.kwargs['audio'], 'cached-file')
        self.assertEqual(self.ui.store.listing(1)[0]['id'], key)
        self.assertEqual(self.busy, set())

    async def test_download_failure_releases_slot(self):
        key = self.ui.store.put('Song', source_id='abcdefghijk')
        self.ui.worker = AsyncMock(side_effect=RuntimeError('provider failed'))
        await self.ui.download(self.update, self.context, key)
        self.assertEqual(self.busy, set())
        self.context.bot.send_audio.assert_not_called()

    async def test_recognition_button_searches_before_downloading(self):
        keyboard = self.ui.recognition_keyboard('Song', 'Artist')
        key = keyboard.inline_keyboard[0][0].callback_data.split(':')[-1]
        self.ui.search = AsyncMock()
        await self.ui.download(self.update, self.context, key)
        self.ui.search.assert_awaited_once_with(self.update, self.context, 'Song Artist')

    async def test_personal_library_not_shown_in_group(self):
        self.update.effective_chat.type = 'group'
        key = self.ui.store.put('Private song')
        self.ui.store.remember(1, key)
        await self.ui.recent(self.update, self.context)
        self.assertNotIn('Private song', str(self.message.reply_text.call_args))

    async def test_inline_results_use_bot_deep_link(self):
        key = self.ui.store.put('Song', source_id='abcdefghijk')
        self.ui.search_tracks = AsyncMock(return_value=[key])
        query = SimpleNamespace(from_user=SimpleNamespace(id=1), query='Song', answer=AsyncMock())
        await self.ui.inline(SimpleNamespace(inline_query=query), self.context)
        result = query.answer.call_args.args[0][0]
        self.assertIn('?start=song_' + key, result.input_message_content.message_text)
        self.assertEqual(self.ui.inline_active, set())

    async def test_start_payload_downloads_selected_track(self):
        self.context.args = ['song_abc']
        self.ui.download = AsyncMock()
        self.assertTrue(await self.ui.start_payload(self.update, self.context))
        self.ui.download.assert_awaited_once_with(self.update, self.context, 'abc')


if __name__ == '__main__':
    unittest.main()
