"""Telegram webhook and observable health endpoint on Render's public port."""
import asyncio
import hashlib
import hmac
import json
import os
import signal
import time

from telegram import Update
from tornado.httpclient import AsyncHTTPClient, HTTPRequest
import tornado.httpserver
import tornado.web


class HealthHandler(tornado.web.RequestHandler):
    def initialize(self, state):
        self.state = state

    def get(self):
        self.set_status(200 if self.state['ready'] else 503)
        self.set_header('Cache-Control', 'no-store')
        queue = self.state.get('queue')
        self.write({
            'status': 'ok' if self.state['ready'] else 'starting',
            'service': 'music-bot',
            'revision': os.environ.get('RENDER_GIT_COMMIT', 'local')[:12],
            'uptime_seconds': max(0, int(time.monotonic() - self.state['started_at'])),
            'queue': queue.qsize() if queue is not None else 0,
            'keepalive': self.state.get('keepalive', 'disabled'),
        })


class TelegramHandler(tornado.web.RequestHandler):
    def initialize(self, bot_application, secret):
        self.application_bot = bot_application
        self.secret = secret

    async def post(self):
        supplied = self.request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not hmac.compare_digest(supplied, self.secret):
            raise tornado.web.HTTPError(403)
        try:
            payload = json.loads(self.request.body)
            if not isinstance(payload, dict) or not isinstance(payload.get('update_id'), int):
                raise ValueError('Invalid update')
            update = Update.de_json(payload, self.application_bot.bot)
        except (ValueError, TypeError, KeyError):
            raise tornado.web.HTTPError(400)
        try:
            self.application_bot.update_queue.put_nowait(update)
        except asyncio.QueueFull:
            raise tornado.web.HTTPError(503)
        self.set_status(200)
        self.finish()


def routes(application, secret, state):
    return tornado.web.Application([
        (r'/healthz', HealthHandler, {'state': state}),
        (r'/', HealthHandler, {'state': state}),
        (r'/telegram', TelegramHandler, {'bot_application': application, 'secret': secret}),
    ])


async def _self_keepalive(url, state, interval):
    """Keep a free web service active without blocking Telegram updates."""
    client = AsyncHTTPClient()
    endpoint = url.rstrip('/') + '/healthz'
    while True:
        await asyncio.sleep(interval)
        try:
            response = await client.fetch(HTTPRequest(
                endpoint,
                method='GET',
                request_timeout=30,
                connect_timeout=15,
                headers={'User-Agent': 'media-bot-self-keepalive/1.0'},
            ))
            state['keepalive'] = 'ok' if response.code == 200 else 'degraded'
        except asyncio.CancelledError:
            raise
        except Exception:
            # GitHub monitoring remains the fallback. A failed self-ping must
            # never crash the bot or interrupt its Telegram webhook.
            state['keepalive'] = 'degraded'


async def serve(application, token, url, port):
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopped.set)
        except (NotImplementedError, RuntimeError):
            pass
    secret = hashlib.sha256(('telegram-webhook:' + token).encode()).hexdigest()
    state = {
        'ready': False,
        'started_at': time.monotonic(),
        'queue': application.update_queue,
        'keepalive': 'starting',
    }
    server = tornado.httpserver.HTTPServer(routes(application, secret, state), max_body_size=1024 * 1024)
    server.listen(port, address='0.0.0.0')
    keepalive_task = None
    try:
        async with application:
            if application.post_init:
                await application.post_init(application)
            await application.start()
            try:
                await application.bot.set_webhook(
                    url=url + '/telegram', secret_token=secret,
                    allowed_updates=Update.ALL_TYPES, drop_pending_updates=False,
                )
                state['ready'] = True
                interval = max(60, int(os.environ.get('SELF_KEEPALIVE_INTERVAL_SECONDS', '480')))
                keepalive_task = asyncio.create_task(
                    _self_keepalive(url, state, interval), name='self-keepalive'
                )
                await stopped.wait()
            finally:
                state['ready'] = False
                if keepalive_task:
                    keepalive_task.cancel()
                    await asyncio.gather(keepalive_task, return_exceptions=True)
                server.stop()
                await application.stop()
    finally:
        server.stop()
        await server.close_all_connections()
