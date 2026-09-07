"""Offline behavioral regression tests; no bot token or network required."""
import ast
import asyncio
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit, urlunsplit

SOURCE = Path(__file__).resolve().parents[1] / 'bot.py'


def namespace():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    names = {'detect_platform', 'normalize_media_url', '_acquire_job',
             '_rate_limited', '_do_video_download', 'handle_audio',
             '_send_media_file', 'download_media', 'InstagramRateLimitError'}
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names]
    env = dict(globals(), _user_busy=set(), _user_hits={},
               _config={'user_limits': True, 'rate_per_min': 2, 'services': {'instagram': True}},
               MAX_PENDING_JOBS=2, MAX_TELEGRAM_BYTES=100, MAX_PHOTO_BYTES=10,
               is_admin=lambda uid: False, track_user=lambda uid: None,
               require_membership=AsyncMock(return_value=True),
               _SHAZAM_AVAILABLE=True, SERVICE_LABELS={'instagram': 'Instagram'},
               logger=logging.getLogger('test'),
               ChatAction=SimpleNamespace(UPLOAD_PHOTO='photo', UPLOAD_VIDEO='video', UPLOAD_VOICE='audio'))
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)] + nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), 'exec'), env)
    return env


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = namespace()
        self.update = SimpleNamespace(effective_user=SimpleNamespace(id=7),
            effective_chat=SimpleNamespace(id=7), message=SimpleNamespace(reply_text=AsyncMock()))
        self.update.effective_message = self.update.message

    def test_urls(self):
        detect = self.env['detect_platform']
        for url in ['https://instagram.com/p/abc', 'https://www.instagram.com/reel/abc']:
            self.assertEqual(detect(url), 'instagram')
        self.assertEqual(detect('https://fb.watch/abc'), 'facebook')
        for url in ['https://example.com/instagram.com/a', 'https://instagram.com.evil.test/a',
                    'https://instagram.com@evil.test/a', 'file://instagram.com/a',
                    'https://instagram.com:9999/a', 'https://[invalid']:
            self.assertIsNone(detect(url), url)

    def test_normalization(self):
        normalize = self.env['normalize_media_url']
        self.assertEqual(normalize('https://instagram.com/p/a?igsh=abc#x'), 'https://instagram.com/p/a')
        self.assertEqual(normalize('https://facebook.com/watch/?v=123#x'), 'https://facebook.com/watch/?v=123')
        with self.assertRaises(ValueError):
            normalize('https://evil.test/instagram.com')

    async def test_duplicate_and_capacity_even_when_rate_limits_disabled(self):
        self.env['_config']['user_limits'] = False
        acquire = self.env['_acquire_job']
        self.assertTrue(await acquire(self.update, 7))
        self.assertFalse(await acquire(self.update, 7))
        self.assertTrue(await acquire(self.update, 8))
        self.assertFalse(await acquire(self.update, 9))

    async def test_status_failure_releases_user(self):
        self.update.message.reply_text.side_effect = RuntimeError('network failed')
        await self.env['_do_video_download'](self.update, SimpleNamespace(), 'https://instagram.com/p/a')
        self.assertNotIn(7, self.env['_user_busy'])
        await self.env['handle_audio'](self.update, SimpleNamespace())
        self.assertNotIn(7, self.env['_user_busy'])

    def test_rate_limit_memory_bounded(self):
        for _ in range(1000):
            self.env['_rate_limited'](7)
        self.assertEqual(len(self.env['_user_hits'][7]), 2)

    async def test_large_photo_and_webm_sent_as_document(self):
        bot = SimpleNamespace(send_document=AsyncMock(), send_photo=AsyncMock())
        with tempfile.TemporaryDirectory() as tmp:
            for name in ['photo.jpg', 'video.webm']:
                path = os.path.join(tmp, name)
                Path(path).write_bytes(b'x' * 11)
                await self.env['_send_media_file'](SimpleNamespace(bot=bot), 7, path)
        self.assertEqual(bot.send_document.await_count, 2)
        bot.send_photo.assert_not_called()

    def test_429_stops_fallbacks(self):
        error = self.env['InstagramRateLimitError']
        fallback = Mock()
        self.env.update(download_video=Mock(side_effect=error('429')),
                        COBALT_API_URL='https://example.test', download_via_cobalt=fallback,
                        download_via_gallery_dl=fallback, _clear_download_dir=Mock())
        with self.assertRaises(error):
            self.env['download_media']('https://instagram.com/p/a', 'unused')
        fallback.assert_not_called()


if __name__ == '__main__':
    unittest.main()
