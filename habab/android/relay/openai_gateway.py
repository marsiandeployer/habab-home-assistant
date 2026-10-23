"""Authenticated, bounded cloud API transport for the owner's Android installation.

No HA access, model substitution, public demo context, or credential logging.
Only hashes of enrolled credentials and short-lived voice keys are retained.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time

from aiohttp import ClientSession, ClientTimeout, ClientWSTimeout, WSMsgType, web

UPSTREAM = 'https://api.openai.com'
ROUTES = {'chat/completions', 'audio/speech', 'audio/transcriptions', 'realtime/client_secrets', 'models'}


class Gateway:
    def __init__(self, key_hash, *, upstream_key=None, control_socket=None):
        if len(key_hash) != 64:
            raise ValueError('Expected an enrolled project key hash')
        self.key_hash = key_hash
        self.upstream_key = upstream_key
        self.control_socket = Path(control_socket) if control_socket else None
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
        control = None
        try:
            if self.control_socket:
                if self.control_socket.exists():
                    info = self.control_socket.stat()
                    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                        raise RuntimeError('Unsafe enrollment socket path')
                    self.control_socket.unlink()
                control = await asyncio.start_unix_server(self.enroll, path=str(self.control_socket), limit=4096)
                self.control_socket.chmod(0o600)
            yield
        finally:
            if control:
                control.close()
                await control.wait_closed()
                self.control_socket.unlink(missing_ok=True)
            self.upstream_key = None
            await self.client.close()

    async def enroll(self, reader, writer):
        """Local operator enrollment: keep the upstream credential only in memory."""
        try:
            peer = writer.get_extra_info('socket')
            _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                raise ValueError('Wrong enrollment owner')
            payload = json.loads(await asyncio.wait_for(reader.readline(), 3))
            key = payload.get('upstream_key')
            if not isinstance(key, str) or not 8 <= len(key) <= 2048 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise ValueError('Invalid enrollment')
            self.upstream_key = key
            writer.write(b'{"enrolled":true}\n')
        except Exception:
            writer.write(b'{"enrolled":false}\n')
        finally:
            try: await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

    async def health(self, request):
        return web.json_response({'service': 'habab-voice-relay', 'ok': True, 'upstreamReady': self.upstream_key is not None})

    async def http(self, request):
        secret, digest = self.authorization(request)
        if not self.owner(digest):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        if self.upstream_key is None:
            return web.json_response({'error': {'code': 'habab_upstream_not_ready'}}, status=503)
        upstream_key = self.upstream_key
        route = request.match_info['route']
        if route not in ROUTES or request.method != ('GET' if route == 'models' else 'POST'):
            return web.json_response({'error': {'code': 'relay_route_not_allowed'}}, status=404)
        if self.inflight >= 6:
            return web.json_response({'error': {'code': 'relay_busy'}}, status=429)
        self.inflight += 1
        try:
            body = await request.read()
            headers = {'Authorization': 'Bearer ' + upstream_key}
            if request.content_type:
                headers['Content-Type'] = request.headers.get('Content-Type', 'application/json')
            async with self.client.request(request.method, UPSTREAM + '/v1/' + route,
                                           data=body or None, headers=headers, allow_redirects=False) as response:
                payload = bytearray()
                async for block in response.content.iter_chunked(65536):
                    payload.extend(block)
                    if len(payload) > 8 * 1024 * 1024:
                        return web.json_response({'error': {'code': 'relay_response_too_large'}}, status=502)
                payload = bytes(payload).replace(upstream_key.encode(), b'[redacted]')
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
        if self.owner(digest) and self.upstream_key is None:
            return web.json_response({'error': {'code': 'habab_upstream_not_ready'}}, status=503)
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
                    headers={'Authorization': 'Bearer ' + (self.upstream_key if self.owner(digest) else secret)}, max_msg_size=256 * 1024,
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

    def app(self):
        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.cleanup_ctx.append(self.lifecycle)
        app.router.add_get('/health', self.health)
        app.router.add_get('/v1/realtime', self.voice)
        app.router.add_route('*', '/v1/{route:.*}', self.http)
        return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    gateway = Gateway(config['key_sha256'], control_socket=args.config.parent / 'control.sock')
    web.run_app(gateway.app(), host='127.0.0.1', port=config.get('port', 8326), access_log=None, print=None, handler_cancellation=True)


if __name__ == '__main__':
    main()
