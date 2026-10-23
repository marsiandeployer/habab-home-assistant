"""Authenticated, bounded cloud API transport for the owner's Android installation.

No HA access, model substitution, public demo context, or credential logging.
Only hashes of enrolled credentials and short-lived voice keys are retained.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import time

from aiohttp import ClientSession, ClientTimeout, ClientWSTimeout, WSMsgType, web

UPSTREAM = 'https://api.openai.com'
ROUTES = {'chat/completions', 'audio/speech', 'audio/transcriptions', 'realtime/client_secrets', 'models'}
TELEGRAM_METHODS = {'getMe', 'getUpdates', 'sendMessage', 'sendChatAction', 'getWebhookInfo'}


class Gateway:
    def __init__(self, key_hash, telegram_hash=None):
        if len(key_hash) != 64:
            raise ValueError('Expected an enrolled project key hash')
        self.key_hash = key_hash
        self.telegram_hash = telegram_hash
        self.poll_lock = asyncio.Lock()
        self.ephemeral = {}
        self.active_voice = 0
        self.inflight = 0
        self.client = None

    def authorization(self, request):
        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer ') or len(header) > 2048:
            return None, None
        secret = header[7:]
        return secret, hashlib.sha256(secret.encode()).hexdigest()

    def owner(self, digest):
        return digest is not None and hmac.compare_digest(digest, self.key_hash)

    async def lifecycle(self, app):
        self.client = ClientSession(timeout=ClientTimeout(total=60), trust_env=False)
        yield
        await self.client.close()

    async def health(self, request):
        return web.json_response({'service': 'habab-voice-relay', 'ok': True})

    async def http(self, request):
        secret, digest = self.authorization(request)
        if not self.owner(digest):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        route = request.match_info['route']
        if route not in ROUTES or request.method != ('GET' if route == 'models' else 'POST'):
            return web.json_response({'error': {'code': 'relay_route_not_allowed'}}, status=404)
        if self.inflight >= 6:
            return web.json_response({'error': {'code': 'relay_busy'}}, status=429)
        self.inflight += 1
        try:
            body = await request.read()
            headers = {'Authorization': 'Bearer ' + secret}
            if request.content_type:
                headers['Content-Type'] = request.headers.get('Content-Type', 'application/json')
            async with self.client.request(request.method, UPSTREAM + '/v1/' + route,
                                           data=body or None, headers=headers, allow_redirects=False) as response:
                payload = bytearray()
                async for block in response.content.iter_chunked(65536):
                    payload.extend(block)
                    if len(payload) > 8 * 1024 * 1024:
                        return web.json_response({'error': {'code': 'relay_response_too_large'}}, status=502)
                payload = bytes(payload)
                if route == 'realtime/client_secrets' and response.status == 200:
                    session = json.loads(payload)
                    value = session.get('value')
                    if not isinstance(value, str) or not value:
                        return web.json_response({'error': {'code': 'relay_invalid_session'}}, status=502)
                    now = time.time()
                    self.ephemeral = {key:item for key,item in self.ephemeral.items() if item[0] > now}
                    model = json.loads(body).get('session', {}).get('model')
                    self.ephemeral[hashlib.sha256(value.encode()).hexdigest()] = (min(now + 60, session.get('expires_at', now + 60)), model)
                return web.Response(body=payload, status=response.status, headers={
                    'Content-Type': response.headers.get('Content-Type', 'application/json'),
                    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        except asyncio.CancelledError:
            raise
        except Exception:
            return web.json_response({'error': {'code': 'relay_upstream_unavailable'}}, status=502)
        finally:
            self.inflight -= 1

    async def voice(self, request):
        secret, digest = self.authorization(request)
        model = request.query.get('model', '')
        ephemeral = self.ephemeral.get(digest)
        allowed = self.owner(digest) or (ephemeral is not None and ephemeral[0] > time.time() and ephemeral[1] == model)
        if not allowed:
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        if not model.startswith('gpt-realtime') or len(model) > 100 or set(request.query) != {'model'}:
            return web.json_response({'error': {'code': 'relay_invalid_model'}}, status=400)
        if self.active_voice >= 2:
            return web.json_response({'error': {'code': 'relay_voice_busy'}}, status=429)
        self.active_voice += 1
        if ephemeral is not None:
            self.ephemeral.pop(digest, None)
        downstream = web.WebSocketResponse(max_msg_size=256 * 1024, heartbeat=20)
        try:
            async with self.client.ws_connect(UPSTREAM + '/v1/realtime', params={'model': model},
                    headers={'Authorization': 'Bearer ' + secret}, max_msg_size=256 * 1024,
                    timeout=ClientWSTimeout(ws_receive=60, ws_close=5), heartbeat=20) as upstream:
                await downstream.prepare(request)
                async def relay(source, target):
                    total = 0
                    async for item in source:
                        if item.type != WSMsgType.TEXT:
                            if item.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR): break
                            continue
                        total += len(item.data.encode())
                        if total > 16 * 1024 * 1024:
                            break
                        await target.send_str(item.data)
                tasks = [asyncio.create_task(relay(downstream, upstream)), asyncio.create_task(relay(upstream, downstream))]
                try:
                    await asyncio.wait(tasks, timeout=180, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks: task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not downstream.prepared:
                return web.json_response({'error': {'code': 'relay_voice_unavailable'}}, status=502)
        finally:
            self.active_voice -= 1
            if downstream.prepared: await downstream.close()
        return downstream

    async def telegram(self, request):
        secret, digest = self.authorization(request)
        if (digest is None or not isinstance(self.telegram_hash, str)
                or not hmac.compare_digest(digest, self.telegram_hash)):
            return web.json_response({'ok': False, 'error_code': 401, 'description': 'Not authorized'}, status=401)
        method = request.match_info['method']
        if method not in TELEGRAM_METHODS or request.method not in {'GET', 'POST'}:
            return web.json_response({'ok': False, 'error_code': 404, 'description': 'Method not available'}, status=404)
        body = await request.read()
        async def forward():
            try:
                async with self.client.request(request.method, 'https://api.telegram.org/bot' + secret + '/' + method,
                        data=body or None, headers={'Content-Type': request.headers.get('Content-Type', 'application/json')},
                        allow_redirects=False) as response:
                    payload = bytearray()
                    async for block in response.content.iter_chunked(65536):
                        payload.extend(block)
                        if len(payload) > 8 * 1024 * 1024:
                            return web.json_response({'ok': False, 'error_code': 502, 'description': 'Response too large'}, status=502)
                    return web.Response(body=bytes(payload), status=response.status,
                                        headers={'Content-Type': 'application/json', 'Cache-Control': 'no-store'})
            except asyncio.CancelledError:
                raise
            except Exception:
                return web.json_response({'ok': False, 'error_code': 502, 'description': 'Upstream unavailable'}, status=502)
        if method == 'getUpdates':
            async with self.poll_lock:
                return await forward()
        return await forward()

    def app(self):
        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.cleanup_ctx.append(self.lifecycle)
        app.router.add_get('/health', self.health)
        app.router.add_route('*', '/telegram/{method}', self.telegram)
        app.router.add_get('/v1/realtime', self.voice)
        app.router.add_route('*', '/v1/{route:.*}', self.http)
        return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    gateway = Gateway(config['key_sha256'], config.get('telegram_sha256'))
    web.run_app(gateway.app(), host='127.0.0.1', port=config.get('port', 8326), access_log=None, print=None, handler_cancellation=True)


if __name__ == '__main__':
    main()
