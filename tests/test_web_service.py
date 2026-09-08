import asyncio
import json
from types import SimpleNamespace
from tornado.testing import AsyncHTTPTestCase
from web_service import routes


class WebTests(AsyncHTTPTestCase):
    def get_app(self):
        self.state = {'ready': True}
        self.queue = asyncio.Queue(maxsize=1)
        return routes(SimpleNamespace(bot=None, update_queue=self.queue), 'test-secret', self.state)

    def test_health(self):
        response = self.fetch('/healthz')
        self.assertEqual(response.code, 200)
        self.assertEqual(json.loads(response.body)['status'], 'ok')
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
