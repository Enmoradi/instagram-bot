import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from tornado.testing import AsyncHTTPTestCase
from web_service import _self_keepalive, routes


class KeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def test_self_keepalive_updates_health_state(self):
        called = asyncio.Event()

        class Client:
            async def fetch(self, request):
                called.set()
                return SimpleNamespace(code=200)

        state = {'keepalive': 'starting'}
        with patch('web_service.AsyncHTTPClient', return_value=Client()):
            task = asyncio.create_task(_self_keepalive('https://bot.example', state, 0))
            await asyncio.wait_for(called.wait(), 1)
            self.assertEqual(state['keepalive'], 'ok')
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class WebTests(AsyncHTTPTestCase):
    def get_app(self):
        self.state = {
            'ready': True,
            'started_at': time.monotonic() - 5,
            'keepalive': 'ok',
        }
        self.queue = asyncio.Queue(maxsize=1)
        self.state['queue'] = self.queue
        return routes(SimpleNamespace(bot=None, update_queue=self.queue), 'test-secret', self.state)

    def test_health(self):
        response = self.fetch('/healthz')
        self.assertEqual(response.code, 200)
        self.assertEqual(json.loads(response.body)['status'], 'ok')
        payload = json.loads(response.body)
        self.assertGreaterEqual(payload['uptime_seconds'], 5)
        self.assertEqual(payload['queue'], 0)
        self.assertEqual(payload['keepalive'], 'ok')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_starting_is_not_healthy(self):
        self.state['ready'] = False
        self.assertEqual(self.fetch('/healthz').code, 503)

    def test_webhook_requires_secret(self):
        self.assertEqual(self.fetch('/telegram', method='POST', body='{"update_id":1}').code, 403)

    def test_valid_webhook_enqueues_update(self):
        response = self.fetch('/telegram', method='POST', body='{"update_id":1}',
                              headers={'X-Telegram-Bot-Api-Secret-Token':'test-secret'})
        self.assertEqual(response.code, 200)
        self.assertEqual(self.queue.get_nowait().update_id, 1)

    def test_full_queue_requests_retry(self):
        self.queue.put_nowait(object())
        response = self.fetch('/telegram', method='POST', body='{"update_id":1}',
                              headers={'X-Telegram-Bot-Api-Secret-Token':'test-secret'})
        self.assertEqual(response.code, 503)

    def test_invalid_payload_rejected(self):
        response = self.fetch('/telegram', method='POST', body='[]',
                              headers={'X-Telegram-Bot-Api-Secret-Token':'test-secret'})
        self.assertEqual(response.code, 400)
