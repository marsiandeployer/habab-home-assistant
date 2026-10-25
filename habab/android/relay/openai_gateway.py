"""Authenticated, bounded Gemini API transport for the owner's Android installation.

No HA access, model substitution, public demo context, or credential logging.
Habab credentials are hashed; Google credentials remain process-memory only.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import time
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, ClientWSTimeout, WSMsgType, web

GEMINI_TEXT_BASE = 'https://generativelanguage.googleapis.com/v1beta/models'
GEMINI_TOKEN_URL = 'https://generativelanguage.googleapis.com/v1beta/auth_tokens'
GEMINI_CONSTRAINED_UPSTREAM = 'wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContentConstrained'
GEMINI_MODEL = 'gemini-3.8-live'
GEMINI_TEXT_MODEL = 'gemini-3.1-flash-lite'
GEMINI_WEBSOCKET = 'wss://habab-voice.wpmix.net/gemini/ws'
TOKEN_RESPONSE_MAX = 64 * 1024
VOICE_FIRST_FRAME_TIMEOUT = 10
VOICE_SETUP_MAX_BYTES = 64 * 1024
BROWSER_VOICE_AUTH_TYPE = 'habab.voice.auth.v1'
LEGACY_OPENAI_ROUTES = {'chat/completions', 'audio/speech', 'audio/transcriptions', 'realtime/client_secrets', 'models'}
TELEGRAM_METHODS = ('getMe', 'getUpdates', 'getChatMember', 'sendMessage', 'sendChatAction')
TELEGRAM_BOT_TOKEN = re.compile(r'[0-9]{1,20}:[A-Za-z0-9_-]{20,256}\Z')
TELEGRAM_API_BASE = 'https://api.telegram.org/bot'
TELEGRAM_REQUEST_MAX = 1024 * 1024
TELEGRAM_RESPONSE_MAX = 8 * 1024 * 1024
TELEGRAM_LONG_POLL_MAX = 25
TELEGRAM_REQUEST_TIMEOUT = 20
TELEGRAM_LONG_POLL_REQUEST_TIMEOUT = 35


def exact_web_origin(value):
    if not isinstance(value, str) or not value or value == '*':
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
        return (parsed.scheme in {'http', 'https'} and bool(parsed.hostname) and bool(parsed.netloc)
                and parsed.username is None and parsed.password is None and parsed.path == ''
                and (port is None or 1 <= port <= 65535)
                and not parsed.query and not parsed.fragment and value == f'{parsed.scheme}://{parsed.netloc}')
    except ValueError:
        return False


async def redacted_stream(content, secret, max_bytes):
    """Stream upstream bytes while redacting a secret even across chunk boundaries."""
    pending = b''
    total = 0
    overlap = max(1, len(secret))
    async for block in content.iter_chunked(16384):
        total += len(block)
        if total > max_bytes:
            raise ValueError('relay_response_too_large')
        combined = pending + block
        cutoff = max(0, len(combined) - overlap + 1)
        search_from = 0
        while True:
            index = combined.find(secret, search_from)
            if index < 0 or index >= cutoff:
                break
            cutoff = max(cutoff, index + len(secret))
            search_from = index + len(secret)
        safe = combined[:cutoff].replace(secret, b'[redacted]')
        pending = combined[cutoff:]
        if safe:
            yield safe
    if pending:
        yield pending.replace(secret, b'[redacted]')


class Gateway:
    def __init__(self, key_hash, *, upstream_key=None, control_socket=None, browser_origins=()):
        if len(key_hash) != 64:
            raise ValueError('Expected an enrolled project key hash')
        if not isinstance(browser_origins, (list, tuple, set, frozenset)):
            raise ValueError('Expected a list of exact browser origins')
        if any(not exact_web_origin(origin) for origin in browser_origins):
            raise ValueError('Invalid browser origin')
        self.key_hash = key_hash
        self.upstream_key = upstream_key
        self.control_socket = Path(control_socket) if control_socket else None
        self.browser_origins = frozenset(browser_origins)
        self.gemini_credentials = {}
        self.active_voice = 0
        self.inflight = 0
        self.client = None

    def authorization(self, request):
        authorization = request.headers.get('Authorization', '')
        api_key = request.headers.get('x-goog-api-key', '')
        if bool(authorization) == bool(api_key):
            return None, None
        if authorization:
            if not authorization.startswith('Bearer ') or len(authorization) > 2048:
                return None, None
            secret = authorization[7:]
        else:
            if len(api_key) > 2048:
                return None, None
            secret = api_key
        if not secret:
            return None, None
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
            self.gemini_credentials.clear()
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

    async def capabilities(self, request):
        _, digest = self.authorization(request)
        if not self.owner(digest):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        if request.query:
            return web.json_response({'error': {'code': 'relay_invalid_query'}}, status=400)
        return web.json_response({
            'provider': 'habab',
            'chat': {'provider': 'gemini', 'protocol': 'google-generative-language-v1beta',
                     'api_base': 'https://habab-voice.wpmix.net/gemini/v1beta',
                     'models': [GEMINI_TEXT_MODEL], 'default_model': GEMINI_TEXT_MODEL,
                     'streaming': True},
            'realtime': {'provider': 'gemini', 'transport': 'habab', 'models': [GEMINI_MODEL],
                         'default_model': GEMINI_MODEL, 'protocol': 'google-generative-language-bidi-v1beta',
                         'bootstrap': '/v1/realtime/bootstrap', 'websocket': '/gemini/ws',
                         'browser_auth': {'enabled': bool(self.browser_origins),
                                          'mode': 'first_text_frame', 'type': BROWSER_VOICE_AUTH_TYPE}},
            'telegram': {'protocol': 'telegram-bot-api-relay-v1', 'base': '/telegram',
                         'methods': list(TELEGRAM_METHODS)},
        }, headers={'Cache-Control': 'no-store'})

    async def telegram(self, request):
        auth_values = request.headers.getall('Authorization', [])
        if len(auth_values) != 1:
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        _, digest = self.authorization(request)
        if not self.owner(digest):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        if request.query:
            return web.json_response({'error': {'code': 'telegram_invalid_query'}}, status=400)

        method = request.match_info.get('method', '')
        if method not in TELEGRAM_METHODS:
            return web.json_response({'error': {'code': 'telegram_method_not_allowed'}}, status=404)
        allowed_methods = ('GET', 'POST') if method == 'getMe' else ('POST',)
        if request.method not in allowed_methods:
            return web.json_response({'error': {'code': 'telegram_http_method_not_allowed'}}, status=405)

        token_values = request.headers.getall('X-Habab-Telegram-Bot-Token', [])
        if (len(token_values) != 1 or len(token_values[0]) > 256
                or TELEGRAM_BOT_TOKEN.fullmatch(token_values[0]) is None):
            return web.json_response({'error': {'code': 'telegram_invalid_bot_token'}}, status=400)
        bot_token = token_values[0]

        body = b''
        if method == 'getMe' and request.method == 'GET':
            if request.content_length not in (None, 0) or request.headers.get('Transfer-Encoding'):
                return web.json_response({'error': {'code': 'telegram_invalid_request'}}, status=400)
        else:
            if request.content_type != 'application/json':
                return web.json_response({'error': {'code': 'telegram_invalid_request'}}, status=400)
            payload = bytearray()
            async for block in request.content.iter_chunked(65536):
                payload.extend(block)
                if len(payload) > TELEGRAM_REQUEST_MAX:
                    return web.json_response({'error': {'code': 'telegram_request_too_large'}}, status=413)
            body = bytes(payload)
            try:
                decoded = json.loads(body)
                if not body or not isinstance(decoded, dict):
                    raise ValueError
                if method == 'getMe' and decoded:
                    raise ValueError
                if method == 'getUpdates':
                    timeout = decoded.get('timeout', 0)
                    if (isinstance(timeout, bool) or not isinstance(timeout, int)
                            or not 0 <= timeout <= TELEGRAM_LONG_POLL_MAX):
                        raise ValueError
            except (ValueError, UnicodeError, TypeError):
                return web.json_response({'error': {'code': 'telegram_invalid_request'}}, status=400)

        if self.inflight >= 6:
            return web.json_response({'error': {'code': 'relay_busy'}}, status=429)
        self.inflight += 1
        timeout_seconds = (TELEGRAM_LONG_POLL_REQUEST_TIMEOUT if method == 'getUpdates'
                           else TELEGRAM_REQUEST_TIMEOUT)
        upstream_url = f'{TELEGRAM_API_BASE}{bot_token}/{method}'
        try:
            timeout = ClientTimeout(total=timeout_seconds, connect=10, sock_connect=10,
                                    sock_read=TELEGRAM_LONG_POLL_MAX + 5 if method == 'getUpdates' else 15)
            async with self.client.request(request.method, upstream_url,
                    headers={'Content-Type': 'application/json'} if body else {},
                    data=body if body else None, allow_redirects=False, timeout=timeout) as response:
                payload = bytearray()
                async for block in response.content.iter_chunked(65536):
                    payload.extend(block)
                    if len(payload) > TELEGRAM_RESPONSE_MAX:
                        return web.json_response({'error': {'code': 'telegram_response_too_large'}}, status=502)
                safe = bytes(payload).replace(bot_token.encode(), b'[redacted]')
                try:
                    parsed = json.loads(safe)
                    if not isinstance(parsed, dict):
                        raise ValueError
                except (ValueError, UnicodeError, TypeError):
                    return web.json_response({'error': {'code': 'telegram_invalid_upstream_response'}}, status=502)
                headers = {'Content-Type': response.headers.get('Content-Type', 'application/json'),
                           'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
                retry_after = response.headers.get('Retry-After')
                if retry_after is not None and re.fullmatch(r'\d{1,6}', retry_after):
                    headers['Retry-After'] = retry_after
                return web.Response(body=safe, status=response.status, headers=headers)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return web.json_response({'error': {'code': 'telegram_upstream_timeout'}}, status=504)
        except Exception:
            return web.json_response({'error': {'code': 'telegram_upstream_unavailable'}}, status=502)
        finally:
            self.inflight -= 1

    async def gemini_bootstrap(self, request):
        service_key, digest = self.authorization(request)
        if not self.owner(digest):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        if request.query:
            return web.json_response({'error': {'code': 'relay_invalid_query'}}, status=400)
        if self.upstream_key is None:
            return web.json_response({'error': {'code': 'habab_upstream_not_ready'}}, status=503)
        if self.active_voice >= 2:
            return web.json_response({'error': {'code': 'relay_voice_busy'}}, status=429)
        try:
            body = await request.json()
            model, setup = body.get('model'), body.get('setup')
            if (set(body) != {'model', 'setup'} or model != GEMINI_MODEL or not isinstance(setup, dict)
                    or setup.get('model') != 'models/' + GEMINI_MODEL):
                raise ValueError
            encoded = json.dumps(setup, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            encoded_bytes = encoded.encode('utf-8')
            if len(encoded_bytes) > 64 * 1024 or service_key.encode() in encoded_bytes:
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            return web.json_response({'error': {'code': 'relay_invalid_model_or_setup'}}, status=400)
        now = time.time()
        token_request = {'uses': 1, 'expireTime': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 180)),
                         'newSessionExpireTime': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 60)),
                         'bidiGenerateContentSetup': setup}
        self.gemini_credentials = {key: item for key, item in self.gemini_credentials.items() if item['expires'] > now}
        if len(self.gemini_credentials) >= 8:
            return web.json_response({'error': {'code': 'relay_voice_busy'}}, status=429)
        if self.inflight >= 6:
            return web.json_response({'error': {'code': 'relay_busy'}}, status=429)
        self.inflight += 1
        try:
            async with self.client.post(GEMINI_TOKEN_URL, headers={'x-goog-api-key': self.upstream_key},
                                        json=token_request, allow_redirects=False) as response:
                if response.status != 200:
                    return web.json_response({'error': {'code': 'relay_upstream_unavailable'}}, status=502)
                token_payload = await response.content.read(TOKEN_RESPONSE_MAX + 1)
                if len(token_payload) > TOKEN_RESPONSE_MAX:
                    return web.json_response({'error': {'code': 'relay_invalid_upstream_response'}}, status=502)
                google_token = json.loads(token_payload)
            if not isinstance(google_token, dict):
                return web.json_response({'error': {'code': 'relay_invalid_upstream_response'}}, status=502)
            google_value = google_token.get('name')
            if not isinstance(google_value, str) or not google_value or len(google_value) > 2048:
                return web.json_response({'error': {'code': 'relay_invalid_upstream_response'}}, status=502)
        except asyncio.CancelledError:
            raise
        except Exception:
            return web.json_response({'error': {'code': 'relay_upstream_unavailable'}}, status=502)
        finally:
            self.inflight -= 1
        value = secrets.token_urlsafe(32)
        self.gemini_credentials[hashlib.sha256(value.encode()).hexdigest()] = {
            'expires': now + 60, 'model': model, 'setup': encoded, 'google_token': google_value,
        }
        return web.json_response({'provider': 'gemini', 'transport': 'habab', 'model': model,
                                  'value': value, 'expires_at': int(now + 60),
                                  'websocket_url': GEMINI_WEBSOCKET, 'setup': setup},
                                 headers={'Cache-Control': 'no-store'})

    async def gemini_text(self, request):
        service_key, digest = self.authorization(request)
        if not self.owner(digest):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        if self.upstream_key is None:
            return web.json_response({'error': {'code': 'habab_upstream_not_ready'}}, status=503)
        upstream_key = self.upstream_key
        match = re.fullmatch(
            r'/gemini/v1beta/models/(gemini-3\.1-flash-lite):(generateContent|streamGenerateContent)',
            request.path,
        )
        if request.method != 'POST' or match is None:
            return web.json_response({'error': {'code': 'relay_route_not_allowed'}}, status=404)
        action = match.group(2)
        streaming = action == 'streamGenerateContent'
        if ((streaming and (len(request.query) != 1 or request.query.get('alt') != 'sse'))
                or (not streaming and len(request.query) != 0)):
            return web.json_response({'error': {'code': 'relay_invalid_query'}}, status=400)
        if request.content_type != 'application/json':
            return web.json_response({'error': {'code': 'relay_invalid_request'}}, status=400)
        try:
            body = await request.read()
            if (not body or len(body) > 2 * 1024 * 1024 or service_key.encode() in body
                    or not isinstance(json.loads(body), dict)):
                raise ValueError
        except (ValueError, UnicodeError, TypeError):
            return web.json_response({'error': {'code': 'relay_invalid_request'}}, status=400)
        if self.inflight >= 6:
            return web.json_response({'error': {'code': 'relay_busy'}}, status=429)
        self.inflight += 1
        upstream_url = GEMINI_TEXT_BASE + '/' + GEMINI_TEXT_MODEL + ':' + action
        query = {'alt': 'sse'} if streaming else None
        result = None
        try:
            async with self.client.post(upstream_url, params=query,
                    headers={'x-goog-api-key': upstream_key, 'Content-Type': 'application/json'},
                    data=body, allow_redirects=False, timeout=ClientTimeout(total=120, sock_read=60)) as response:
                headers = {'Content-Type': response.headers.get('Content-Type', 'application/json'),
                           'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
                if streaming and response.status == 200:
                    result = web.StreamResponse(status=response.status, headers=headers)
                    await result.prepare(request)
                    needle = upstream_key.encode()
                    try:
                        async for block in redacted_stream(response.content, needle, 16 * 1024 * 1024):
                            await result.write(block)
                    except ValueError:
                        await result.write(b'event: error\ndata: {"error":{"code":"relay_response_too_large"}}\n\n')
                    await result.write_eof()
                    return result
                payload = bytearray()
                async for block in response.content.iter_chunked(65536):
                    payload.extend(block)
                    if len(payload) > 8 * 1024 * 1024:
                        return web.json_response({'error': {'code': 'relay_response_too_large'}}, status=502)
                safe = bytes(payload).replace(upstream_key.encode(), b'[redacted]')
                return web.Response(body=safe, status=response.status, headers=headers)
        except asyncio.CancelledError:
            raise
        except Exception:
            if result is not None and result.prepared:
                try:
                    await result.write(b'event: error\ndata: {"error":{"code":"relay_upstream_unavailable"}}\n\n')
                    await result.write_eof()
                except Exception:
                    pass
                return result
            return web.json_response({'error': {'code': 'relay_upstream_unavailable'}}, status=502)
        finally:
            self.inflight -= 1

    async def gemini_voice(self, request):
        if request.query:
            return web.json_response({'error': {'code': 'relay_invalid_query'}}, status=400)
        header = request.headers.get('Authorization', '')
        browser_mode = 'Authorization' not in request.headers
        voice_slot = False
        item = None
        if browser_mode:
            origin = request.headers.get('Origin')
            if origin not in self.browser_origins:
                return web.json_response({'error': {'code': 'relay_origin_not_allowed'}}, status=403)
            if self.active_voice >= 2:
                return web.json_response({'error': {'code': 'relay_voice_busy'}}, status=429)
            # Bound unauthenticated upgraded sockets to the same two voice slots.
            self.active_voice += 1
            voice_slot = True
        else:
            secret = header[6:] if header.startswith('Token ') and len(header) <= 2048 else ''
            digest = hashlib.sha256(secret.encode()).hexdigest() if secret else None
            item = self.gemini_credentials.get(digest)
            if item is None or item['expires'] <= time.time() or self.upstream_key is None:
                return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
            if self.active_voice >= 2:
                return web.json_response({'error': {'code': 'relay_voice_busy'}}, status=429)
            # A valid native credential is consumed as soon as an upgrade is accepted.
            self.gemini_credentials.pop(digest, None)
            self.active_voice += 1
            voice_slot = True
        downstream = web.WebSocketResponse(max_msg_size=256 * 1024, heartbeat=20)
        try:
            await downstream.prepare(request)
            try:
                first = await downstream.receive(timeout=VOICE_FIRST_FRAME_TIMEOUT)
            except asyncio.TimeoutError:
                reason = b'auth_timeout' if browser_mode else b'invalid_setup'
                await downstream.close(code=1008, message=reason)
                return downstream
            if first.type != WSMsgType.TEXT or len(first.data.encode()) > VOICE_SETUP_MAX_BYTES:
                await downstream.close(code=1008, message=b'invalid_auth_frame' if browser_mode else b'invalid_setup')
                return downstream
            try:
                frame = json.loads(first.data)
                if browser_mode:
                    valid_envelope = (isinstance(frame, dict) and set(frame) == {'type', 'credential', 'setup'}
                                      and frame.get('type') == BROWSER_VOICE_AUTH_TYPE
                                      and isinstance(frame.get('credential'), str)
                                      and 1 <= len(frame['credential']) <= 2048)
                    if not valid_envelope:
                        await downstream.close(code=1008, message=b'invalid_auth_frame')
                        return downstream
                    secret = frame['credential']
                    digest = hashlib.sha256(secret.encode()).hexdigest()
                    item = self.gemini_credentials.get(digest)
                    if item is None or item['expires'] <= time.time() or self.upstream_key is None:
                        await downstream.close(code=1008, message=b'relay_not_authorized')
                        return downstream
                    # Consume before setup comparison: even a mismatched setup cannot replay the grant.
                    self.gemini_credentials.pop(digest, None)
                    setup = frame['setup']
                else:
                    setup = frame.get('setup') if isinstance(frame, dict) and set(frame) == {'setup'} else None
                canonical = json.dumps(setup, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            except (ValueError, TypeError, AttributeError):
                if browser_mode:
                    await downstream.close(code=1008, message=b'invalid_auth_frame')
                    return downstream
                setup, canonical = None, None
            if not isinstance(setup, dict) or canonical != item['setup']:
                await downstream.close(code=1008, message=b'invalid_setup')
                return downstream
            google_token = item.pop('google_token')
            async with self.client.ws_connect(GEMINI_CONSTRAINED_UPSTREAM,
                    headers={'Authorization': 'Token ' + google_token}, max_msg_size=256 * 1024,
                    timeout=ClientWSTimeout(ws_receive=60, ws_close=5), heartbeat=20) as upstream:
                google_token = None
                setup_frame = (json.dumps({'setup': setup}, ensure_ascii=False, separators=(',', ':'))
                               if browser_mode else first.data)
                await upstream.send_str(setup_frame)

                async def relay(source, target, *, from_upstream=False):
                    total = 0
                    async for message in source:
                        if message.type == WSMsgType.TEXT:
                            total += len(message.data.encode())
                            if total > 16 * 1024 * 1024:
                                break
                            await target.send_str(message.data)
                        elif message.type == WSMsgType.BINARY:
                            total += len(message.data)
                            if total > 16 * 1024 * 1024:
                                break
                            await target.send_bytes(message.data)
                        elif message.type == WSMsgType.ERROR:
                            if from_upstream:
                                raise RuntimeError('voice_transport_failed')
                            break
                        elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED):
                            if from_upstream and source.close_code not in (1000, 1001):
                                raise RuntimeError('voice_transport_failed')
                            break
                    if from_upstream and source.close_code not in (1000, 1001):
                        raise RuntimeError('voice_transport_failed')

                tasks = [asyncio.create_task(relay(downstream, upstream)),
                         asyncio.create_task(relay(upstream, downstream, from_upstream=True))]
                try:
                    done, _ = await asyncio.wait(tasks, timeout=180, return_when=asyncio.FIRST_COMPLETED)
                    if any(task.exception() is not None for task in done if not task.cancelled()):
                        raise RuntimeError('voice_transport_failed')
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not downstream.prepared:
                return web.json_response({'error': {'code': 'relay_voice_unavailable'}}, status=502)
            try:
                await downstream.close(code=1011, message=b'relay_voice_unavailable')
            except Exception:
                pass
        finally:
            if voice_slot:
                self.active_voice -= 1
            if downstream.prepared:
                await downstream.close()
        return downstream

    async def legacy_http(self, request):
        _, digest = self.authorization(request)
        if not self.owner(digest):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        route = request.match_info['route']
        if route in LEGACY_OPENAI_ROUTES:
            return web.json_response({'error': {'code': 'habab_api_upgrade_required'}}, status=426,
                                     headers={'Cache-Control': 'no-store'})
        return web.json_response({'error': {'code': 'relay_route_not_allowed'}}, status=404)

    def app(self):
        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.cleanup_ctx.append(self.lifecycle)
        app.router.add_get('/health', self.health)
        app.router.add_get('/v1/capabilities', self.capabilities)
        app.router.add_route('*', '/telegram/{method}', self.telegram)
        app.router.add_post('/v1/realtime/bootstrap', self.gemini_bootstrap)
        app.router.add_get('/gemini/ws', self.gemini_voice)
        app.router.add_route('*', '/gemini/{tail:.*}', self.gemini_text)
        app.router.add_route('*', '/v1/{route:.*}', self.legacy_http)
        return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    gateway = Gateway(config['key_sha256'], control_socket=args.config.parent / 'control.sock',
                      browser_origins=config.get('browser_origins', []))
    web.run_app(gateway.app(), host='127.0.0.1', port=config.get('port', 8326), access_log=None, print=None, handler_cancellation=True)


if __name__ == '__main__':
    main()
