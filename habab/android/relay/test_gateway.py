"""Exercise auth, route boundaries and full response forwarding without live keys."""
import hashlib
import asyncio
import json
from pathlib import Path
import re
import stat
import tempfile
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

from aiohttp.client import ClientSession
from aiohttp import WSMsgType, web
from aiohttp.client_exceptions import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from beta_codes import BetaCodes, create_config
from openai_gateway import Gateway, load_private_config


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def test_private_config_accepts_optional_voice_service_key_only_from_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'relay.json'
            path.write_text(json.dumps({
                'key_sha256': hashlib.sha256(b'synthetic-owner').hexdigest(),
                'voice_service_key': 'synthetic-voice-service',
            }), encoding='utf-8')
            path.chmod(0o600)
            config = load_private_config(path)
            self.assertEqual(config['voice_service_key'], 'synthetic-voice-service')
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'private_config_file_required'):
                load_private_config(path)

    async def asyncSetUp(self):
        original_request = ClientSession._request
        async def guarded_request(session, method, url, **kwargs):
            host = urlsplit(str(url)).hostname
            if host not in {'127.0.0.1', 'localhost', '::1'}:
                raise AssertionError('relay tests block non-loopback outbound requests')
            return await original_request(session, method, url, **kwargs)
        self.outbound_guard = patch.object(ClientSession, '_request', guarded_request)
        self.outbound_guard.start()
        self.calls = []
        self.echo_secret = False
        self.large_response = False
        self.rotate_upstream_key = False
        self.oversized_token_response = False
        self.fail_gemini_connect = False
        self.fail_openai_handshake = False
        self.close_gemini_with = None
        self.telegram_calls = []
        self.telegram_status = 200
        self.telegram_payload = {'ok': True, 'result': {'id': 12}}
        self.telegram_retry_after = None
        self.telegram_delay = 0
        self.telegram_started = asyncio.Event()
        self.telegram_finished = asyncio.Event()
        self.telegram_large_response = False
        self.deepseek_calls = []
        self.deepseek_status = 200
        self.deepseek_echo_secret = False
        self.deepseek_stream = False
        self.gemini_frames = []
        self.gemini_connections = 0
        self.openai_frames = []
        self.openai_connections = 0
        async def upstream(request):
            if request.path == '/deepseek/chat/completions':
                body = await request.read()
                self.deepseek_calls.append((request.method, request.path, request.query_string,
                                            request.headers.get('Authorization'),
                                            request.headers.get('x-goog-api-key'), body))
                if self.deepseek_stream:
                    result = web.StreamResponse(status=self.deepseek_status,
                                                headers={'Content-Type': 'text/event-stream'})
                    await result.prepare(request)
                    echo = 'synthetic-deepseek-vendor' if self.deepseek_echo_secret else 'stream-ok'
                    middle = len(echo) // 2
                    await result.write(b'data: {"content":"' + echo[:middle].encode())
                    await result.write(echo[middle:].encode() + b'"}\n\n')
                    await result.write_eof()
                    return result
                if self.deepseek_echo_secret:
                    return web.json_response({'echo': request.headers.get('Authorization')},
                                             status=self.deepseek_status)
                return web.json_response({'id': 'chatcmpl-test', 'model': 'deepseek-flash',
                                          'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}]},
                                         status=self.deepseek_status)
            if request.path.startswith('/bot'):
                body = await request.read()
                bot_path, _, bot_method = request.path.rpartition('/')
                fixed_bot_path = bool(re.fullmatch(r'/bot[0-9]{1,20}:[A-Za-z0-9_-]{20,256}', bot_path))
                self.telegram_calls.append((request.method, fixed_bot_path, bot_method, body,
                                            request.headers.get('Authorization'),
                                            request.headers.get('x-goog-api-key'),
                                            request.headers.get('X-Habab-Telegram-Bot-Token') is not None))
                self.telegram_started.set()
                try:
                    if self.telegram_delay:
                        await asyncio.sleep(self.telegram_delay)
                    if self.telegram_large_response:
                        return web.Response(body=b'x' * 128, content_type='application/json')
                    headers = {'Retry-After': str(self.telegram_retry_after)} if self.telegram_retry_after else None
                    return web.json_response(self.telegram_payload, status=self.telegram_status, headers=headers)
                finally:
                    self.telegram_finished.set()
            received_key = request.headers.get('x-goog-api-key')
            self.calls.append((request.path, received_key, request.query_string))
            if request.path.endswith('/auth_tokens'):
                self.token_body = await request.json()
                self.token_headers = dict(request.headers)
                if self.oversized_token_response:
                    return web.Response(body=b'x' * (64 * 1024 + 1), content_type='application/json')
                return web.json_response({'name': 'synthetic-google-token'})
            if self.rotate_upstream_key:
                self.gateway.upstream_key = 'synthetic-replaced-vendor'
            if request.path.endswith(':streamGenerateContent'):
                result = web.StreamResponse(status=200, headers={'Content-Type': 'text/event-stream'})
                await result.prepare(request)
                echo = received_key if self.echo_secret or self.rotate_upstream_key else 'synthetic-vendor'
                middle = len(echo) // 2
                await result.write(b'data: {"echo":"' + echo[:middle].encode())
                await result.write(echo[middle:].encode() + b'"}\n\n')
                await result.write_eof()
                return result
            if self.echo_secret:
                return web.json_response({'echo': received_key})
            if self.large_response:
                return web.Response(body=b'x' * 150000)
            return web.json_response({'candidates': [{'content': {'parts': [{'text': 'synthetic Gemini reply'}]}}]})
        async def gemini_upstream(request):
            if self.fail_gemini_connect:
                return web.Response(status=503)
            self.gemini_connections += 1
            self.gemini_frames.append(('auth', request.headers.get('Authorization')))
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for message in ws:
                self.gemini_frames.append(('frame', message.data))
                if self.close_gemini_with is not None:
                    code, reason = self.close_gemini_with
                    await ws.close(code=code, message=reason)
                    return ws
                if message.type == WSMsgType.BINARY:
                    await ws.send_bytes(message.data)
                else:
                    await ws.send_str(message.data)
            return ws
        async def openai_upstream(request):
            self.openai_connections += 1
            self.openai_frames.append(('auth', request.headers.get('Authorization'),
                                       request.headers.get('OpenAI-Safety-Identifier')))
            if self.fail_openai_handshake:
                return web.Response(status=503, reason='SYNTHETIC_PRIVATE_FAILURE_DETAIL')
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    self.openai_frames.append(('frame', message.data))
                    await ws.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    self.openai_frames.append(('binary', message.data))
                    await ws.send_bytes(message.data)
            return ws
        app = web.Application()
        app.router.add_route('*', '/{path:.*}', upstream)
        app.router.add_get('/gemini', gemini_upstream)
        app.router.add_get('/openai/realtime', openai_upstream)
        self.upstream = TestServer(app, handler_cancellation=True)
        await self.upstream.start_server()
        self.browser_origin = 'https://habab-demo.wpmix.net'
        self.control_dir = tempfile.TemporaryDirectory()
        self.control_path = Path(self.control_dir.name) / 'control.sock'
        self.gateway = Gateway(hashlib.sha256(b'synthetic-owner').hexdigest(), upstream_key='synthetic-vendor',
                               text_upstream_key='synthetic-deepseek-vendor',
                               browser_origins=[self.browser_origin], control_socket=self.control_path)
        self.patch = patch('openai_gateway.GEMINI_TEXT_BASE', str(self.upstream.make_url('/v1beta/models')).rstrip('/'))
        self.patch.start()
        self.gemini_patch = patch('openai_gateway.GEMINI_CONSTRAINED_UPSTREAM', str(self.upstream.make_url('/gemini')))
        self.gemini_patch.start()
        self.token_patch = patch('openai_gateway.GEMINI_TOKEN_URL', str(self.upstream.make_url('/v1beta/auth_tokens')))
        self.token_patch.start()
        self.telegram_patch = patch('openai_gateway.TELEGRAM_API_BASE', str(self.upstream.make_url('/bot')).rstrip('/'))
        self.telegram_patch.start()
        self.deepseek_patch = patch('openai_gateway.DEEPSEEK_CHAT_COMPLETIONS',
                                    str(self.upstream.make_url('/deepseek/chat/completions')))
        self.deepseek_patch.start()
        self.openai_patch = patch('openai_gateway.OPENAI_REALTIME_UPSTREAM',
                                  str(self.upstream.make_url('/openai/realtime')).replace('http://', 'ws://'))
        self.openai_patch.start()
        self.client = TestClient(TestServer(self.gateway.app(), handler_cancellation=True))
        await self.client.start_server()
        self.voice_requests = []
        self.voice_reply_bodies = []
        self.voice_status = 200
        self.voice_response_override = None
        async def voice_backend(request):
            body = await request.read()
            self.voice_requests.append((request.method, request.path,
                                        request.headers.get('Authorization'), body))
            if request.path == '/v1/capabilities':
                return web.json_response({
                    'provider': 'habab', 'chat': {'provider': 'gemini'},
                    'realtime': {'provider': 'gemini', 'transport': 'habab',
                                 'models': ['gemini-3.8-live'], 'default_model': 'gemini-3.8-live',
                                 'websocket': '/gemini/ws',
                                 'browser_auth': {'enabled': True, 'mode': 'first_text_frame',
                                                  'type': 'habab.voice.auth.v1'}},
                    'telegram': {},
                })
            if self.voice_response_override is not None:
                self.voice_reply_bodies.append(self.voice_response_override)
                return web.Response(body=self.voice_response_override, content_type='application/json')
            if request.path == '/v1/realtime/bootstrap':
                payload = json.loads(body)
                response = web.json_response({
                    'provider': 'gemini', 'transport': 'habab', 'model': payload['model'],
                    'value': 'synthetic-opaque-voice-credential', 'expires_at': 1_900_000_000,
                    'websocket_url': 'wss://habab-voice.wpmix.net/gemini/ws',
                    'setup': payload['setup'],
                }, status=self.voice_status)
                self.voice_reply_bodies.append(response.body)
                return response
            return web.json_response({'error': 'not found'}, status=404)
        voice_app = web.Application()
        voice_app.router.add_route('*', '/{path:.*}', voice_backend)
        self.voice_server = TestServer(voice_app)
        await self.voice_server.start_server()
        self.voice_url_patch = patch('openai_gateway.VOICE_RELAY_URL', str(self.voice_server.make_url('/')).rstrip('/'))
        self.voice_url_patch.start()
        beta_config_path = Path(self.control_dir.name) / 'beta.json'
        create_config(beta_config_path)
        self.beta_codes = BetaCodes(beta_config_path)
        self.bridge_gateway = Gateway(
            hashlib.sha256(b'synthetic-owner').hexdigest(),
            browser_origins=[self.browser_origin], beta_codes=self.beta_codes,
            voice_service_key='synthetic-voice-service',
        )
        self.bridge_client = TestClient(TestServer(self.bridge_gateway.app(), handler_cancellation=True))
        await self.bridge_client.start_server()

    async def asyncTearDown(self):
        await self.bridge_client.close()
        await self.client.close()
        await self.voice_server.close()
        await self.upstream.close()
        self.voice_url_patch.stop()
        self.patch.stop()
        self.gemini_patch.stop()
        self.token_patch.stop()
        self.telegram_patch.stop()
        self.deepseek_patch.stop()
        self.openai_patch.stop()
        self.outbound_guard.stop()
        self.control_dir.cleanup()

    async def test_voice_capabilities_bridge_uses_only_server_service_credential(self):
        response = await self.bridge_client.get(
            '/v1/capabilities', headers={'Authorization': 'Bearer synthetic-owner'})
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload['realtime']['websocket'], '/gemini/ws')
        setup = {'model': 'models/gemini-3.8-live'}
        bootstrap = await self.bridge_client.post(
            '/v1/realtime/bootstrap', headers={'Authorization': 'Bearer synthetic-owner'},
            json={'model': 'gemini-3.8-live', 'setup': setup})
        self.assertEqual(bootstrap.status, 200)
        self.assertEqual(self.voice_requests, [
            ('GET', '/v1/capabilities', 'Bearer synthetic-voice-service', b''),
            ('POST', '/v1/realtime/bootstrap', 'Bearer synthetic-voice-service',
             json.dumps({'model': 'gemini-3.8-live', 'setup': setup},
                        ensure_ascii=False, separators=(',', ':')).encode()),
        ])
        self.assertNotIn('synthetic-voice-service', await response.text())
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    async def test_hb1_bootstrap_bridge_returns_validated_voice_payload_unchanged(self):
        installation_id = '123e4567-e89b-42d3-a456-426614174000'
        enrollment = await self.bridge_client.post(
            '/beta/enroll', json={'installation_id': installation_id})
        self.assertEqual(enrollment.status, 200)
        beta_token = (await enrollment.json())['code']
        setup = {'model': 'models/gemini-3.8-live', 'tools': [{'functionDeclarations': []}]}
        # Replace only the synthetic backend's bootstrap route for byte-level
        # equality; this does not alter the production fixed destination.
        self.voice_response_override = b'{"provider":"gemini","transport":"habab","model":"gemini-3.8-live","value":"synthetic-opaque-voice-credential","expires_at":1900000000,"websocket_url":"wss://habab-voice.wpmix.net/gemini/ws","setup":{"model":"models/gemini-3.8-live","tools":[{"functionDeclarations":[]}]}}'
        response = await self.bridge_client.post(
            '/v1/realtime/bootstrap', headers={'Authorization': 'Bearer ' + beta_token},
            json={'model': 'gemini-3.8-live', 'setup': setup})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), self.voice_response_override)
        self.assertEqual(self.voice_requests[-1][0:3], (
            'POST', '/v1/realtime/bootstrap', 'Bearer synthetic-voice-service'))
        self.assertEqual(json.loads(self.voice_requests[-1][3]), {'model': 'gemini-3.8-live', 'setup': setup})
        self.assertNotIn('synthetic-voice-service', self.voice_requests[-1][3].decode())
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    async def test_voice_bootstrap_accepts_escaped_unicode_wire_over_64k(self):
        content = 'К' * 8192 + '"' * 8192
        setup = {'model': 'models/gemini-3.8-live',
                 'history': [{'role': 'user', 'content': content}]}
        request_body = json.dumps({'model': 'gemini-3.8-live', 'setup': setup}).encode('utf-8')
        canonical_setup = json.dumps(setup, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')
        self.assertGreater(len(request_body), 64 * 1024)
        self.assertLessEqual(len(canonical_setup), 64 * 1024)

        response = await self.bridge_client.post(
            '/v1/realtime/bootstrap', headers={'Authorization': 'Bearer synthetic-owner',
                                                'Content-Type': 'application/json'},
            data=request_body)
        self.assertEqual(response.status, 200)
        returned = await response.read()
        self.assertGreater(len(returned), 64 * 1024)
        self.assertEqual(returned, self.voice_reply_bodies[-1])
        self.assertLessEqual(len(self.voice_requests[-1][3]), 64 * 1024)
        self.assertEqual(json.loads(self.voice_requests[-1][3])['setup'], setup)

    async def test_voice_bootstrap_collects_fragmented_chunked_json_and_rejects_oversize(self):
        setup = {'model': 'models/gemini-3.8-live', 'history': 'Кириллица'}
        body = json.dumps({'model': 'gemini-3.8-live', 'setup': setup}).encode()

        async def fragmented_body():
            for part in (body[:7], body[7:19], body[19:]):
                yield part
                await asyncio.sleep(0)

        response = await self.bridge_client.post(
            '/v1/realtime/bootstrap', headers={'Authorization': 'Bearer synthetic-owner',
                                                'Content-Type': 'application/json'},
            data=fragmented_body())
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(await response.read())['setup'], setup)
        forwarded_count = len(self.voice_requests)

        async def oversized_body():
            yield b' ' * (256 * 1024)
            await asyncio.sleep(0)
            yield b' ' * (256 * 1024 + 1)

        response = await self.bridge_client.post(
            '/v1/realtime/bootstrap', headers={'Authorization': 'Bearer synthetic-owner',
                                                'Content-Type': 'application/json'},
            data=oversized_body())
        self.assertEqual(response.status, 413)
        self.assertEqual((await response.json())['error']['code'], 'relay_request_too_large')
        self.assertEqual(len(self.voice_requests), forwarded_count)

    async def test_voice_bridge_rejects_invalid_client_and_invalid_backend_payload(self):
        setup = {'model': 'models/gemini-3.8-live'}
        response = await self.bridge_client.post(
            '/v1/realtime/bootstrap', headers={'Authorization': 'Bearer wrong'},
            json={'model': 'gemini-3.8-live', 'setup': setup})
        self.assertEqual(response.status, 401)
        self.assertEqual(self.voice_requests, [])
        self.voice_response_override = b'{"provider":"gemini","transport":"habab","model":"gemini-3.8-live","value":"bad","expires_at":1,"websocket_url":"wss://attacker.example/ws","setup":{"model":"models/gemini-3.8-live"}}'
        response = await self.bridge_client.post(
            '/v1/realtime/bootstrap', headers={'Authorization': 'Bearer synthetic-owner'},
            json={'model': 'gemini-3.8-live', 'setup': setup})
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())['error']['code'], 'relay_invalid_voice_response')

    async def test_voice_bridge_maps_voice_readiness_and_busy_states(self):
        self.voice_status = 503
        response = await self.bridge_client.post(
            '/v1/realtime/bootstrap', headers={'Authorization': 'Bearer synthetic-owner'},
            json={'model': 'gemini-3.8-live', 'setup': {'model': 'models/gemini-3.8-live'}})
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())['error']['code'], 'habab_upstream_not_ready')
        self.voice_status = 429
        response = await self.bridge_client.post(
            '/v1/realtime/bootstrap', headers={'Authorization': 'Bearer synthetic-owner'},
            json={'model': 'gemini-3.8-live', 'setup': {'model': 'models/gemini-3.8-live'}})
        self.assertEqual(response.status, 429)
        self.assertEqual((await response.json())['error']['code'], 'relay_voice_busy')

    async def demo_openai_credential(self, session_hash=None):
        reader, writer = await asyncio.open_unix_connection(self.control_path)
        payload = {'purpose': 'demo_openai_bootstrap', 'upstream_key': 'synthetic-openai-vendor',
                   'session_hash': session_hash or hashlib.sha256(b'synthetic-demo-session').hexdigest()}
        writer.write(json.dumps(payload).encode() + b'\n')
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return response

    async def bootstrap(self, setup=None, headers=None, path='/v1/realtime/bootstrap'):
        setup = setup or {'model': 'models/gemini-3.8-live', 'tools': [{'functionDeclarations': []}]}
        return await self.client.post(path, headers=headers or {'Authorization': 'Bearer synthetic-owner'},
                                      json={'model': 'gemini-3.8-live', 'setup': setup})

    async def browser_connect(self, credential, setup, *, origin=None, path='/gemini/ws'):
        ws = await self.client.ws_connect(path, origin=origin or self.browser_origin)
        await ws.send_json({'type': 'habab.voice.auth.v1', 'credential': credential, 'setup': setup})
        return ws

    async def browser_credential(self):
        response = await self.bootstrap()
        self.assertEqual(response.status, 200)
        return await response.json()

    async def assert_policy_close(self, ws, code, reason):
        message = await ws.receive(timeout=3)
        self.assertEqual(message.type, WSMsgType.CLOSE)
        self.assertEqual(message.data, code)
        self.assertEqual(message.extra, reason)
        await ws.close()

    async def test_capabilities_are_authenticated_and_explicit_about_gemini(self):
        response = await self.client.get('/v1/capabilities', headers={'Authorization': 'Bearer wrong'})
        self.assertEqual(response.status, 401)
        response = await self.client.get('/v1/capabilities', headers={'Authorization': 'Bearer synthetic-owner'})
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload['chat']['protocol'], 'google-generative-language-v1beta')
        self.assertEqual(payload['chat']['models'], ['gemini-3.1-flash-lite', 'gemini-3.8-flash'])
        self.assertEqual(payload['chat']['default_model'], 'gemini-3.1-flash-lite')
        self.assertEqual(payload['chat']['api_base'], 'https://habab-voice.wpmix.net/gemini/v1beta')
        self.assertEqual(payload['realtime']['transport'], 'habab')
        self.assertEqual(payload['realtime']['protocol'], 'google-generative-language-bidi-v1beta')
        self.assertEqual(payload['realtime']['browser_auth'], {
            'enabled': True, 'mode': 'first_text_frame', 'type': 'habab.voice.auth.v1'})
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    async def test_capabilities_bootstrap_and_voice_reject_query_parameters(self):
        headers = {'Authorization': 'Bearer synthetic-owner'}
        response = await self.client.get('/v1/capabilities?key=untrusted', headers=headers)
        self.assertEqual(response.status, 400)
        response = await self.bootstrap(headers=headers, path='/v1/realtime/bootstrap?key=untrusted')
        self.assertEqual(response.status, 400)
        payload_response = await self.bootstrap(headers=headers)
        value = (await payload_response.json())['value']
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect('/gemini/ws?key=untrusted', headers={'Authorization': 'Token ' + value})
        self.assertEqual(self.gemini_frames, [])

    async def test_oversized_google_token_response_does_not_issue_relay_credential(self):
        self.oversized_token_response = True
        response = await self.bootstrap()
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())['error']['code'], 'relay_invalid_upstream_response')
        self.assertEqual(self.gateway.gemini_credentials, {})

    async def test_gemini_bootstrap_is_single_use_and_proxies_native_frames(self):
        response = await self.bootstrap()
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual((payload['provider'], payload['transport'], payload['model']),
                         ('gemini', 'habab', 'gemini-3.8-live'))
        self.assertEqual(payload['websocket_url'], 'wss://habab-voice.wpmix.net/gemini/ws')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        digest = hashlib.sha256(payload['value'].encode()).hexdigest()
        self.assertIn(digest, self.gateway.gemini_credentials)
        self.assertNotIn(payload['value'], repr(self.gateway.gemini_credentials))
        self.assertNotIn('synthetic-vendor', repr(self.gateway.gemini_credentials))
        self.assertEqual(self.token_headers.get('x-goog-api-key'), 'synthetic-vendor')
        self.assertEqual(self.token_body['uses'], 1)
        self.assertEqual(self.token_body['bidiGenerateContentSetup'], payload['setup'])
        ws = await self.client.ws_connect('/gemini/ws', headers={'Authorization': 'Token ' + payload['value']})
        await ws.send_json({'setup': payload['setup']})
        setup = await ws.receive_json(timeout=3)
        self.assertEqual(setup, {'setup': payload['setup']})
        self.assertEqual(self.gemini_frames[0], ('auth', 'Token synthetic-google-token'))
        self.assertEqual(json.loads(self.gemini_frames[1][1]), {'setup': payload['setup']})
        await ws.send_bytes(b'gemini-binary-frame')
        echoed = await ws.receive(timeout=3)
        self.assertEqual((echoed.type, echoed.data), (WSMsgType.BINARY, b'gemini-binary-frame'))
        await ws.close()
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect('/gemini/ws', headers={'Authorization': 'Token ' + payload['value']})

    async def test_browser_first_frame_auth_strips_credential_and_preserves_native_google_frames(self):
        payload = await self.browser_credential()
        ws = await self.browser_connect(payload['value'], payload['setup'])
        setup = await ws.receive_json(timeout=3)
        self.assertEqual(setup, {'setup': payload['setup']})
        self.assertEqual(self.gemini_connections, 1)
        self.assertEqual(self.gemini_frames[0], ('auth', 'Token synthetic-google-token'))
        self.assertEqual(json.loads(self.gemini_frames[1][1]), {'setup': payload['setup']})
        serialized_upstream = repr(self.gemini_frames)
        self.assertNotIn(payload['value'], serialized_upstream)
        self.assertNotIn('synthetic-vendor', serialized_upstream)
        await ws.send_json({'toolCall': {'functionCalls': [{'id': 'call-7', 'name': 'ask_home_assistant',
                                                            'args': {'text': 'Проверь устройство'}}]}})
        tool = await ws.receive_json(timeout=3)
        self.assertEqual(tool['toolCall']['functionCalls'][0]['id'], 'call-7')
        tool_result = {'toolResponse': {'functionResponses': [{'id': 'call-7', 'name': 'ask_home_assistant',
                                                               'response': {'answer': 'Нашёл устройство'}}]}}
        await ws.send_json(tool_result)
        self.assertEqual(await ws.receive_json(timeout=3), tool_result)
        await ws.send_bytes(b'gemini-binary-frame')
        echoed = await ws.receive(timeout=3)
        self.assertEqual((echoed.type, echoed.data), (WSMsgType.BINARY, b'gemini-binary-frame'))
        await ws.close()

    async def test_browser_origin_is_exact_allowlisted_and_disabled_by_default(self):
        disabled = Gateway(hashlib.sha256(b'synthetic-owner').hexdigest(), upstream_key='synthetic-vendor')
        self.assertEqual(disabled.browser_origins, frozenset())
        async with TestClient(TestServer(disabled.app())) as client:
            response = await client.get('/v1/capabilities', headers={'Authorization': 'Bearer synthetic-owner'})
            self.assertFalse((await response.json())['realtime']['browser_auth']['enabled'])
            with self.assertRaises(WSServerHandshakeError) as disabled_ws:
                await client.ws_connect('/gemini/ws', origin=self.browser_origin)
            self.assertEqual(disabled_ws.exception.status, 403)
        payload = await self.browser_credential()
        with self.assertRaises(WSServerHandshakeError) as missing:
            await self.client.ws_connect('/gemini/ws')
        self.assertEqual(missing.exception.status, 403)
        with self.assertRaises(WSServerHandshakeError) as disallowed:
            await self.client.ws_connect('/gemini/ws', origin='https://attacker.example')
        self.assertEqual(disallowed.exception.status, 403)
        self.assertEqual(self.gemini_connections, 0)
        self.assertIn(hashlib.sha256(payload['value'].encode()).hexdigest(), self.gateway.gemini_credentials)
        with self.assertRaises(ValueError):
            Gateway(hashlib.sha256(b'synthetic-owner').hexdigest(), browser_origins=['*'])
        with self.assertRaises(ValueError):
            Gateway(hashlib.sha256(b'synthetic-owner').hexdigest(), browser_origins=['https://example.test/path'])
        with self.assertRaises(ValueError):
            Gateway(hashlib.sha256(b'synthetic-owner').hexdigest(), browser_origins=['https://example.test:65536'])

    async def test_browser_invalid_expired_and_replayed_credentials_close_without_upstream(self):
        payload = await self.browser_credential()
        ws = await self.browser_connect('invalid-synthetic-token', payload['setup'])
        await self.assert_policy_close(ws, 1008, 'relay_not_authorized')

        expired = await self.browser_credential()
        digest = hashlib.sha256(expired['value'].encode()).hexdigest()
        self.gateway.gemini_credentials[digest]['expires'] = 0
        ws = await self.browser_connect(expired['value'], expired['setup'])
        await self.assert_policy_close(ws, 1008, 'relay_not_authorized')

        replay = await self.browser_credential()
        first = await self.browser_connect(replay['value'], replay['setup'])
        self.assertEqual(await first.receive_json(timeout=3), {'setup': replay['setup']})
        second = await self.browser_connect(replay['value'], replay['setup'])
        await self.assert_policy_close(second, 1008, 'relay_not_authorized')
        self.assertEqual(self.gemini_connections, 1)
        await first.close()

    async def test_browser_credential_has_exactly_one_winner_when_replayed_concurrently(self):
        payload = await self.browser_credential()
        clients = await asyncio.gather(
            self.client.ws_connect('/gemini/ws', origin=self.browser_origin),
            self.client.ws_connect('/gemini/ws', origin=self.browser_origin),
        )
        frame = {'type': 'habab.voice.auth.v1', 'credential': payload['value'], 'setup': payload['setup']}
        await asyncio.gather(*(client.send_json(frame) for client in clients))
        replies = await asyncio.gather(*(client.receive(timeout=3) for client in clients))
        self.assertEqual(sum(reply.type == WSMsgType.TEXT for reply in replies), 1)
        self.assertEqual(sum(reply.type == WSMsgType.CLOSE and reply.data == 1008
                             and reply.extra == 'relay_not_authorized' for reply in replies), 1)
        self.assertEqual(self.gemini_connections, 1)
        self.assertNotIn(hashlib.sha256(payload['value'].encode()).hexdigest(), self.gateway.gemini_credentials)
        await asyncio.gather(*(client.close() for client in clients))

    async def test_browser_setup_mismatch_consumes_credential_and_never_connects_upstream(self):
        payload = await self.browser_credential()
        wrong_setup = {**payload['setup'], 'systemInstruction': {'parts': [{'text': 'mutated'}]}}
        ws = await self.browser_connect(payload['value'], wrong_setup)
        await self.assert_policy_close(ws, 1008, 'invalid_setup')
        digest = hashlib.sha256(payload['value'].encode()).hexdigest()
        self.assertNotIn(digest, self.gateway.gemini_credentials)
        replay = await self.browser_connect(payload['value'], payload['setup'])
        await self.assert_policy_close(replay, 1008, 'relay_not_authorized')
        self.assertEqual(self.gemini_connections, 0)

    async def test_browser_invalid_first_frames_close_safely_without_consuming_other_grants(self):
        payload = await self.browser_credential()
        frames = [
            'not-json',
            {'type': 'wrong.type', 'credential': payload['value'], 'setup': payload['setup']},
            {'type': 'habab.voice.auth.v1', 'credential': payload['value'], 'setup': payload['setup'], 'extra': True},
            {'type': 'habab.voice.auth.v1', 'credential': '', 'setup': payload['setup']},
        ]
        for frame in frames:
            ws = await self.client.ws_connect('/gemini/ws', origin=self.browser_origin)
            if isinstance(frame, str):
                await ws.send_str(frame)
            else:
                await ws.send_json(frame)
            await self.assert_policy_close(ws, 1008, 'invalid_auth_frame')
        self.assertEqual(self.gemini_connections, 0)
        self.assertIn(hashlib.sha256(payload['value'].encode()).hexdigest(), self.gateway.gemini_credentials)

        payload = await self.browser_credential()
        ws = await self.client.ws_connect('/gemini/ws', origin=self.browser_origin)
        await ws.send_bytes(b'not-json')
        await self.assert_policy_close(ws, 1008, 'invalid_auth_frame')

        payload = await self.browser_credential()
        ws = await self.client.ws_connect('/gemini/ws', origin=self.browser_origin)
        await ws.send_str('x' * (64 * 1024 + 1))
        await self.assert_policy_close(ws, 1008, 'invalid_auth_frame')
        self.assertEqual(self.gemini_connections, 0)

    async def test_browser_first_frame_timeout_is_bounded_and_no_upstream_starts(self):
        payload = await self.browser_credential()
        with patch('openai_gateway.VOICE_FIRST_FRAME_TIMEOUT', 0.02):
            ws = await self.client.ws_connect('/gemini/ws', origin=self.browser_origin)
            await self.assert_policy_close(ws, 1008, 'auth_timeout')
        self.assertEqual(self.gemini_connections, 0)
        self.assertIn(hashlib.sha256(payload['value'].encode()).hexdigest(), self.gateway.gemini_credentials)

    async def test_browser_provisional_connections_are_bounded_and_released(self):
        first = await self.client.ws_connect('/gemini/ws', origin=self.browser_origin)
        second = await self.client.ws_connect('/gemini/ws', origin=self.browser_origin)
        self.assertEqual(self.gateway.active_voice, 2)
        with self.assertRaises(WSServerHandshakeError) as busy:
            await self.client.ws_connect('/gemini/ws', origin=self.browser_origin)
        self.assertEqual(busy.exception.status, 429)
        await first.close()
        await second.close()
        self.assertEqual(self.gateway.active_voice, 0)

    async def test_browser_upstream_failure_uses_sanitized_1011_close(self):
        payload = await self.browser_credential()
        self.fail_gemini_connect = True
        ws = await self.browser_connect(payload['value'], payload['setup'])
        await self.assert_policy_close(ws, 1011, 'relay_voice_unavailable')
        self.assertEqual(self.gemini_connections, 0)
        self.assertNotIn(payload['value'], repr(self.gemini_frames))

    async def test_native_upstream_abnormal_close_is_sanitized_as_1011(self):
        response = await self.bootstrap()
        payload = await response.json()
        self.close_gemini_with = (1011, b'synthetic-upstream-secret')
        ws = await self.client.ws_connect('/gemini/ws',
                                          headers={'Authorization': 'Token ' + payload['value']})
        await ws.send_json({'setup': payload['setup']})
        await self.assert_policy_close(ws, 1011, 'relay_voice_unavailable')
        self.assertNotIn('synthetic-upstream-secret', repr(self.gemini_frames))
        self.assertEqual(self.gemini_frames[0], ('auth', 'Token synthetic-google-token'))

    async def test_gemini_bootstrap_rejects_other_models_and_non_owner_keys(self):
        response = await self.bootstrap(headers={'Authorization': 'Bearer synthetic-vendor'})
        self.assertEqual(response.status, 401)
        response = await self.bootstrap({'model': 'models/gpt-realtime-mini'})
        self.assertEqual(response.status, 400)
        response = await self.bootstrap({'model': 'models/gemini-3.8-live', 'systemInstruction': 'synthetic-owner'})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.gemini_frames, [])

    async def test_gemini_websocket_rejects_mutated_setup_and_consumes_credential(self):
        response = await self.bootstrap()
        payload = await response.json()
        ws = await self.client.ws_connect('/gemini/ws', headers={'Authorization': 'Token ' + payload['value']})
        await ws.send_json({'setup': {'model': 'models/gemini-3.8-live'}})
        message = await ws.receive(timeout=3)
        self.assertEqual(message.type, WSMsgType.CLOSE)
        self.assertEqual(self.gemini_frames, [])
        response = await self.client.get('/gemini/ws', headers={'Authorization': 'Token ' + payload['value']})
        self.assertEqual(response.status, 401)

    async def test_expired_gemini_credential_is_rejected(self):
        response = await self.bootstrap()
        payload = await response.json()
        digest = hashlib.sha256(payload['value'].encode()).hexdigest()
        self.gateway.gemini_credentials[digest]['expires'] = 0
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect('/gemini/ws', headers={'Authorization': 'Token ' + payload['value']})

    async def test_unknown_keys_cannot_reach_upstream(self):
        for path in ['/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent', '/v1/models']:
            response = await self.client.get(path, headers={'Authorization': 'Bearer wrong'})
            self.assertEqual(response.status, 401)
        self.assertEqual(self.calls, [])

    async def test_owner_cannot_proxy_arbitrary_route_or_method(self):
        headers = {'Authorization': 'Bearer synthetic-owner'}
        for method, path in [('GET', '/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent'),
                             ('POST', '/gemini/v1beta/models/gemini-3.8-live:generateContent'),
                             ('GET', '/gemini/v1beta/models/gemini-3.1-flash-lite:streamGenerateContent?alt=sse'),
                             ('GET', '/v1/files')]:
            response = await self.client.request(method, path, headers=headers)
            self.assertNotEqual(response.status, 200)
        self.assertEqual(self.calls, [])

    async def test_large_response_is_forwarded_to_eof(self):
        self.large_response = True
        response = await self.client.post('/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent',
            headers={'x-goog-api-key': 'synthetic-owner'}, json={'contents': []})
        self.assertEqual(response.status, 200)
        self.assertEqual(len(await response.read()), 150000)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.calls[-1][1], 'synthetic-vendor')
        self.assertEqual(self.calls[-1][0], '/v1beta/models/gemini-3.1-flash-lite:generateContent')

    async def test_gemini_native_request_uses_google_key_header_and_preserves_response(self):
        body = {'contents': [{'role': 'user', 'parts': [{'text': 'synthetic question'}]}]}
        response = await self.client.post('/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent',
            headers={'x-goog-api-key': 'synthetic-owner'}, json=body)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {'candidates': [{'content': {'parts': [{'text': 'synthetic Gemini reply'}]}}]})
        self.assertEqual(self.calls[-1][1], 'synthetic-vendor')
        self.assertNotIn('synthetic-owner', repr(self.calls))
        self.assertNotIn('synthetic-vendor', self.calls[-1][2])

    async def test_gemini_text_allowlist_forwards_each_model_to_fixed_google_host(self):
        headers = {'x-goog-api-key': 'synthetic-owner'}
        for model in ('gemini-3.1-flash-lite', 'gemini-3.8-flash'):
            response = await self.client.post(
                f'/gemini/v1beta/models/{model}:generateContent',
                headers=headers, json={'contents': []})
            self.assertEqual(response.status, 200)
            self.assertEqual(self.calls[-1], (
                f'/v1beta/models/{model}:generateContent', 'synthetic-vendor', ''))
        self.assertEqual(len(self.calls), 2)

    async def test_gemini_sse_is_streamed_and_redacts_secret_across_chunks(self):
        response = await self.client.post(
            '/gemini/v1beta/models/gemini-3.1-flash-lite:streamGenerateContent?alt=sse',
            headers={'x-goog-api-key': 'synthetic-owner'}, json={'contents': []})
        payload = await response.read()
        self.assertEqual(response.status, 200)
        self.assertIn(b'text/event-stream', response.headers['Content-Type'].encode())
        self.assertIn(b'[redacted]', payload)
        self.assertNotIn(b'synthetic-vendor', payload)
        self.assertEqual(self.calls[-1][1], 'synthetic-vendor')

    async def test_text_redaction_uses_the_key_snapshot_for_json_and_split_sse(self):
        self.echo_secret = True
        self.rotate_upstream_key = True
        json_response = await self.client.post(
            '/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent',
            headers={'x-goog-api-key': 'synthetic-owner'}, json={'contents': []})
        json_payload = await json_response.read()
        self.assertEqual(json_response.status, 200)
        self.assertIn(b'[redacted]', json_payload)
        self.assertNotIn(b'synthetic-vendor', json_payload)
        self.assertEqual(self.gateway.upstream_key, 'synthetic-replaced-vendor')

        self.gateway.upstream_key = 'synthetic-vendor'
        sse_response = await self.client.post(
            '/gemini/v1beta/models/gemini-3.1-flash-lite:streamGenerateContent?alt=sse',
            headers={'x-goog-api-key': 'synthetic-owner'}, json={'contents': []})
        sse_payload = await sse_response.read()
        self.assertEqual(sse_response.status, 200)
        self.assertIn(b'[redacted]', sse_payload)
        self.assertNotIn(b'synthetic-vendor', sse_payload)
        self.assertNotIn(b'synthetic-replaced-vendor', sse_payload)

    async def test_gemini_text_rejects_bad_route_query_and_request_without_upstream(self):
        headers = {'x-goog-api-key': 'synthetic-owner'}
        cases = [('/gemini/v1beta/models/gemini-3.8-live:generateContent', {}, 404),
                 ('/gemini/v1beta/models/gemini-3.8-flash-preview:generateContent', {}, 404),
                 ('/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent?key=bad', {'contents': []}, 400),
                 ('/gemini/v1beta/models/gemini-3.1-flash-lite:streamGenerateContent', {'contents': []}, 400),
                 ('/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent',
                  {'contents': [{'parts': [{'text': 'synthetic-owner'}]}]}, 400),
                 ('/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent', [], 400)]
        for path, body, status in cases:
            response = await self.client.post(path, headers=headers, json=body)
            self.assertEqual(response.status, status)
        self.assertEqual(self.calls, [])

    async def test_vendor_key_does_not_authorize_service_requests(self):
        response = await self.client.get('/v1/capabilities', headers={'x-goog-api-key': 'synthetic-vendor'})
        self.assertEqual(response.status, 401)
        self.assertEqual(self.calls, [])

    async def test_even_a_malformed_upstream_echo_cannot_return_the_vendor_key(self):
        self.echo_secret = True
        response = await self.client.post('/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent',
            headers={'x-goog-api-key': 'synthetic-owner'}, json={'contents': []})
        payload = await response.read()
        self.assertNotIn(b'synthetic-vendor', payload)
        self.assertIn(b'[redacted]', payload)

    async def test_missing_ram_enrollment_is_explicit_and_never_forwards_service_key(self):
        self.gateway.upstream_key = None
        response = await self.client.post('/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent',
            headers={'x-goog-api-key': 'synthetic-owner'}, json={'contents': []})
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())['error']['code'], 'habab_upstream_not_ready')
        self.assertEqual(self.calls, [])

    async def test_managed_chat_route_rejects_unknown_model_and_other_legacy_routes_without_fallback(self):
        headers = {'Authorization': 'Bearer synthetic-owner'}
        response = await self.client.post('/v1/chat/completions', headers=headers, json={'model': 'gpt-4.1-mini'})
        self.assertEqual(response.status, 404)
        self.assertEqual((await response.json())['error']['code'], 'relay_unknown_model')
        response = await self.client.post('/v1/realtime/client_secrets', headers=headers,
                                          json={'session': {'model': 'gpt-realtime-mini'}})
        self.assertEqual(response.status, 426)
        response = await self.client.get('/v1/realtime?model=gpt-realtime-mini', headers=headers)
        self.assertEqual(response.status, 404)
        self.assertEqual(self.calls, [])

    async def test_demo_openai_grant_is_one_use_and_relays_audio_and_server_owned_session(self):
        payload = await self.demo_openai_credential()
        self.assertTrue(payload['ready'])
        self.assertEqual(payload['websocket_url'], 'wss://habab-voice.wpmix.net/openai/demo/ws')
        self.assertNotIn('synthetic-openai-vendor', repr(self.gateway.demo_openai_credentials))
        self.assertEqual(len(payload['value']), 43)
        ws = await self.client.ws_connect('/openai/demo/ws', origin=self.browser_origin)
        await ws.send_json({'type': 'habab.demo.openai.auth.v1', 'token': payload['value']})
        configured = await ws.receive_json(timeout=3)
        self.assertEqual(configured['type'], 'session.update')
        session = configured['session']
        self.assertEqual(session['model'], 'gpt-realtime-2.1')
        self.assertEqual(session['audio']['input']['format'], {'type': 'audio/pcm', 'rate': 24000})
        self.assertEqual({tool['name'] for tool in session['tools']}, {
            'set_demo_light', 'set_demo_climate', 'activate_demo_scene'})
        self.assertEqual(self.openai_frames[0][1], 'Bearer synthetic-openai-vendor')
        self.assertEqual(self.openai_frames[0][2], hashlib.sha256(b'synthetic-demo-session').hexdigest())
        await ws.send_json({'type': 'input_audio_buffer.append', 'audio': 'AQID'})
        echoed = await ws.receive_json(timeout=3)
        self.assertEqual(echoed, {'type': 'input_audio_buffer.append', 'audio': 'AQID'})
        browser_frames = [json.loads(frame[1]) for frame in self.openai_frames
                          if frame[0] == 'frame' and json.loads(frame[1]).get('type') != 'session.update']
        self.assertEqual(browser_frames[-1]['type'], 'input_audio_buffer.append')
        await ws.close()
        replay = await self.client.ws_connect('/openai/demo/ws', origin=self.browser_origin)
        await replay.send_json({'type': 'habab.demo.openai.auth.v1', 'token': payload['value']})
        denied = await replay.receive(timeout=3)
        self.assertEqual(denied.type, WSMsgType.CLOSE)
        self.assertEqual(denied.data, 1008)
        await replay.close()
        self.assertEqual(self.openai_connections, 1)

    async def test_demo_openai_relay_blocks_client_session_overrides_and_untrusted_origins(self):
        payload = await self.demo_openai_credential()
        response = await self.client.get('/openai/demo/ws', headers={'Origin': 'https://attacker.invalid'})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.openai_connections, 0)
        ws = await self.client.ws_connect('/openai/demo/ws', origin=self.browser_origin)
        await ws.send_json({'type': 'habab.demo.openai.auth.v1', 'token': payload['value']})
        self.assertEqual((await ws.receive_json(timeout=3))['type'], 'session.update')
        await ws.send_json({'type': 'session.update', 'session': {'instructions': 'override'}})
        closed = await ws.receive(timeout=3)
        self.assertEqual(closed.type, WSMsgType.CLOSE)
        self.assertEqual(closed.data, 1008)
        self.assertEqual(closed.extra, 'session_config_is_server_owned')
        self.assertEqual(self.openai_connections, 1)
        forwarded = [json.loads(frame[1]) for frame in self.openai_frames if frame[0] == 'frame']
        self.assertEqual([frame['type'] for frame in forwarded], ['session.update'])
        await ws.close()

    async def test_demo_openai_upstream_failure_logs_only_exception_class_and_status(self):
        self.fail_openai_handshake = True
        grant = await self.demo_openai_credential()
        self.assertTrue(grant['ready'])
        ws = await self.client.ws_connect('/openai/demo/ws', origin=self.browser_origin)
        await ws.send_json({'type': 'habab.demo.openai.auth.v1', 'token': grant['value']})

        with self.assertLogs('openai_gateway', level='WARNING') as captured:
            closed = await ws.receive(timeout=3)

        self.assertEqual(closed.type, WSMsgType.CLOSE)
        self.assertEqual(closed.data, 1011)
        self.assertEqual(closed.extra, 'openai_demo_unavailable')
        self.assertEqual(len(captured.output), 1)
        self.assertIn('exception=WSServerHandshakeError', captured.output[0])
        self.assertIn('status=503', captured.output[0])
        self.assertNotIn('SYNTHETIC_PRIVATE_FAILURE_DETAIL', '\n'.join(captured.output))
        await ws.close()

    async def test_demo_openai_global_budget_expires_at_rolling_window_boundary(self):
        self.gateway.demo_openai_session_limit = 1
        self.gateway.demo_openai_sessions_per_session = 2
        self.gateway.demo_openai_session_window_seconds = 10
        clock = [100.0]
        self.gateway._demo_openai_clock = lambda: clock[0]

        first_hash = hashlib.sha256(b'synthetic-global-session-0').hexdigest()
        grant = await self.demo_openai_credential(first_hash)
        self.assertTrue(grant['ready'])
        ws = await self.client.ws_connect('/openai/demo/ws', origin=self.browser_origin)
        await ws.send_json({'type': 'habab.demo.openai.auth.v1', 'token': grant['value']})
        self.assertEqual((await ws.receive_json(timeout=3))['type'], 'session.update')
        await ws.close()
        self.assertEqual(self.gateway.demo_openai_sessions_started, 1)

        clock[0] = 109.999
        denied = await self.demo_openai_credential(hashlib.sha256(b'synthetic-global-session-1').hexdigest())
        self.assertEqual(denied, {'ready': False})
        self.assertEqual(self.openai_connections, 1)

        # A charge expires exactly at its configured rolling-window boundary.
        clock[0] = 110.0
        next_grant = await self.demo_openai_credential(hashlib.sha256(b'synthetic-global-session-2').hexdigest())
        self.assertTrue(next_grant['ready'])
        next_ws = await self.client.ws_connect('/openai/demo/ws', origin=self.browser_origin)
        await next_ws.send_json({'type': 'habab.demo.openai.auth.v1', 'token': next_grant['value']})
        self.assertEqual((await next_ws.receive_json(timeout=3))['type'], 'session.update')
        await next_ws.close()
        self.assertEqual(self.gateway.demo_openai_sessions_started, 2)
        self.assertEqual(len(self.gateway.demo_openai_session_starts), 1)
        self.assertEqual(self.openai_connections, 2)

    async def test_demo_openai_global_budget_is_atomic_for_concurrent_sessions(self):
        self.gateway.demo_openai_session_limit = 1
        self.gateway.demo_openai_sessions_per_session = 2
        grants = [await self.demo_openai_credential(hashlib.sha256(f'synthetic-concurrent-{index}'.encode()).hexdigest())
                  for index in range(2)]
        self.assertTrue(all(grant['ready'] for grant in grants))

        async def connect(grant):
            ws = await self.client.ws_connect('/openai/demo/ws', origin=self.browser_origin)
            await ws.send_json({'type': 'habab.demo.openai.auth.v1', 'token': grant['value']})
            message = await ws.receive(timeout=3)
            if message.type == WSMsgType.TEXT:
                await ws.close()
                return 'connected'
            await ws.close()
            return 'denied'

        outcomes = await asyncio.gather(*(connect(grant) for grant in grants))
        self.assertCountEqual(outcomes, ['connected', 'denied'])
        self.assertEqual(self.gateway.demo_openai_sessions_started, 1)
        self.assertEqual(len(self.gateway.demo_openai_session_starts), 1)
        self.assertEqual(self.openai_connections, 1)

    async def test_demo_openai_per_session_budget_stops_repeated_session_bootstrap(self):
        self.gateway.demo_openai_session_limit = 4
        self.gateway.demo_openai_sessions_per_session = 2
        self.gateway.demo_openai_session_window_seconds = 10
        clock = [100.0]
        self.gateway._demo_openai_clock = lambda: clock[0]
        session_hash = hashlib.sha256(b'synthetic-same-session').hexdigest()

        async def start_session():
            grant = await self.demo_openai_credential(session_hash)
            self.assertTrue(grant['ready'])
            ws = await self.client.ws_connect('/openai/demo/ws', origin=self.browser_origin)
            await ws.send_json({'type': 'habab.demo.openai.auth.v1', 'token': grant['value']})
            self.assertEqual((await ws.receive_json(timeout=3))['type'], 'session.update')
            await ws.close()

        await start_session()
        await start_session()
        denied = await self.demo_openai_credential(session_hash)
        self.assertEqual(denied, {'ready': False})
        self.assertEqual(len(self.gateway.demo_openai_sessions_by_hash[session_hash]), 2)
        self.assertEqual(self.gateway.demo_openai_sessions_started, 2)

        # Both per-session charges expire with the global window, allowing this cookie again.
        clock[0] = 110.0
        await start_session()
        self.assertEqual(self.gateway.demo_openai_sessions_started, 3)
        self.assertEqual(len(self.gateway.demo_openai_sessions_by_hash[session_hash]), 1)
        self.assertEqual(self.openai_connections, 3)

    async def test_managed_text_capabilities_report_configured_upstream_without_key_material(self):
        headers = {'Authorization': 'Bearer synthetic-owner'}
        response = await self.client.get('/text/capabilities', headers={'Authorization': 'Bearer wrong'})
        self.assertEqual(response.status, 401)
        response = await self.client.get('/text/capabilities', headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {
            'protocol': 'openai_compatible', 'model': 'habab-chat', 'api_path': '/v1',
            'managed': True, 'configured': True, 'ready': True,
            'configured_upstream_provider': 'deepseek',
            'configured_upstream_model': 'deepseek-flash',
        })
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        response = await self.client.get('/v1/models', headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['data'], [
            {'id': 'habab-chat', 'object': 'model', 'owned_by': 'habab'}])
        response = await self.client.get('/v1/models?key=blocked', headers=headers)
        self.assertEqual(response.status, 400)
        response = await self.client.post('/v1/chat/completions',
            headers={'Authorization': 'Bearer wrong'},
            json={'model': 'habab-chat', 'messages': [{'role': 'user', 'content': 'hello'}]})
        self.assertEqual(response.status, 401)
        self.gateway.text_upstream_key = None
        response = await self.client.get('/text/capabilities', headers=headers)
        unavailable = await response.json()
        self.assertEqual(unavailable['ready'], False)
        self.assertNotIn('configured_upstream_provider', unavailable)
        self.assertNotIn('configured_upstream_model', unavailable)
        response = await self.client.post('/v1/chat/completions', headers=headers,
            json={'model': 'habab-chat', 'messages': [{'role': 'user', 'content': 'hello'}]})
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())['error']['code'], 'habab_text_upstream_not_ready')
        self.assertEqual(self.deepseek_calls, [])

    async def test_managed_text_passthrough_maps_alias_and_uses_separate_bearer_key(self):
        headers = {'Authorization': 'Bearer synthetic-owner'}
        request = {'model': 'habab-chat', 'messages': [{'role': 'user', 'content': 'hello'}],
                   'temperature': 0.2, 'max_tokens': 24}
        response = await self.client.post('/v1/chat/completions', headers=headers, json=request)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['choices'][0]['message']['content'], 'OK')
        method, path, query, auth, google_key, body = self.deepseek_calls[0]
        self.assertEqual((method, path, query), ('POST', '/deepseek/chat/completions', ''))
        self.assertEqual(auth, 'Bearer synthetic-deepseek-vendor')
        self.assertIsNone(google_key)
        forwarded = json.loads(body)
        self.assertEqual(forwarded['model'], 'deepseek-flash')
        self.assertEqual(forwarded['messages'], request['messages'])
        self.assertEqual(forwarded['max_tokens'], 24)
        self.assertEqual(self.calls, [])

    async def test_managed_text_stream_redacts_split_upstream_key_and_releases_slot(self):
        self.deepseek_stream = True
        self.deepseek_echo_secret = True
        response = await self.client.post('/v1/chat/completions',
            headers={'Authorization': 'Bearer synthetic-owner'},
            json={'model': 'habab-chat', 'messages': [{'role': 'user', 'content': 'hello'}], 'stream': True})
        payload = await response.read()
        self.assertEqual(response.status, 200)
        self.assertIn(b'[redacted]', payload)
        self.assertNotIn(b'synthetic-deepseek-vendor', payload)
        self.assertEqual(response.headers['Content-Type'], 'text/event-stream')
        self.assertEqual(self.gateway.inflight, 0)

    async def test_managed_text_forced_tool_choices_disable_upstream_thinking(self):
        headers = {'Authorization': 'Bearer synthetic-owner'}
        tool = {'type': 'function', 'function': {
            'name': 'StructuredOutput',
            'parameters': {'type': 'object', 'additionalProperties': False,
                           'properties': {'kind': {'type': 'string', 'enum': ['answer']}},
                           'required': ['kind']},
        }}
        for choice in ('required', {'type': 'function', 'function': {'name': 'StructuredOutput'}}):
            for conflict in ({}, {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high'}):
                with self.subTest(choice=choice, conflict=conflict):
                    request = {'model': 'habab-chat', 'messages': [{'role': 'user', 'content': 'hello'}],
                               'tools': [tool], 'tool_choice': choice, 'temperature': 0.2, **conflict}
                    original = json.loads(json.dumps(request))
                    response = await self.client.post('/v1/chat/completions', headers=headers, json=request)
                    self.assertEqual(response.status, 200)
                    await response.read()
                    expected = {**original, 'model': 'deepseek-flash', 'thinking': {'type': 'disabled'}}
                    if 'reasoning_effort' in original:
                        expected['reasoning_effort'] = 'none'
                    self.assertEqual(json.loads(self.deepseek_calls[-1][5]), expected)
                    self.assertEqual(request, original)
        self.assertEqual(self.calls, [])

    async def test_managed_text_nonforced_and_toolless_options_remain_unchanged(self):
        headers = {'Authorization': 'Bearer synthetic-owner'}
        tool = {'type': 'function', 'function': {'name': 'fixture', 'parameters': {'type': 'object'}}}
        for extra in ({}, {'tools': [tool], 'tool_choice': 'auto'},
                      {'tools': [tool], 'tool_choice': 'none'}, {'tool_choice': 'required'}):
            with self.subTest(extra=extra):
                request = {'model': 'habab-chat', 'messages': [{'role': 'user', 'content': 'hello'}],
                           'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high', **extra}
                original = json.loads(json.dumps(request))
                response = await self.client.post('/v1/chat/completions', headers=headers, json=request)
                self.assertEqual(response.status, 200)
                await response.read()
                self.assertEqual(json.loads(self.deepseek_calls[-1][5]), {**original, 'model': 'deepseek-flash'})
                self.assertEqual(request, original)
        self.assertEqual(self.calls, [])

    async def test_managed_text_upstream_error_is_forwarded_without_google_fallback(self):
        self.deepseek_status = 503
        response = await self.client.post('/v1/chat/completions',
            headers={'Authorization': 'Bearer synthetic-owner'},
            json={'model': 'habab-chat', 'messages': [{'role': 'user', 'content': 'hello'}]})
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())['model'], 'deepseek-flash')
        self.assertEqual(len(self.deepseek_calls), 1)
        self.assertEqual(self.calls, [])

    async def test_telegram_token_does_not_authorize_gemini_service(self):
        response = await self.client.get('/v1/capabilities', headers={'x-goog-api-key': 'synthetic-bot'})
        self.assertEqual(response.status, 401)
        response = await self.client.post('/telegram/deleteWebhook', headers={'Authorization': 'Bearer synthetic-bot'})
        self.assertEqual(response.status, 401)
        self.assertEqual(self.calls, [])

    async def test_capabilities_advertise_narrow_telegram_contract(self):
        response = await self.client.get('/v1/capabilities', headers={'Authorization': 'Bearer synthetic-owner'})
        payload = await response.json()
        self.assertEqual(payload['telegram'], {
            'protocol': 'telegram-bot-api-relay-v1', 'base': '/telegram',
            'methods': ['getMe', 'getUpdates', 'getChatMember', 'getFile', 'sendMessage', 'sendChatAction'],
            'voice': {'download': '/telegram/downloadFile', 'max_bytes': 5 * 1024 * 1024, 'max_duration': 120},
        })
        self.assertEqual(payload['speech']['transcriptions'], '/v1/audio/transcriptions')
        self.assertEqual(payload['speech']['model'], 'habab-transcribe')
        self.assertTrue(payload['speech']['ready'])

    async def test_telegram_auth_token_query_and_method_boundaries_do_not_reach_upstream(self):
        owner = {'Authorization': 'Bearer synthetic-owner'}
        valid_token = '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk'
        cases = [
            ('GET', '/telegram/getMe', {}, 401),
            ('GET', '/telegram/getMe', {**owner, 'X-Habab-Telegram-Bot-Token': 'malformed'}, 400),
            ('GET', '/telegram/getMe?chat_id=123', {**owner, 'X-Habab-Telegram-Bot-Token': valid_token}, 400),
            ('GET', '/telegram/getUpdates', {**owner, 'X-Habab-Telegram-Bot-Token': valid_token}, 405),
            ('POST', '/telegram/notAMethod', {**owner, 'X-Habab-Telegram-Bot-Token': valid_token}, 404),
        ]
        for method, path, headers, status in cases:
            response = await self.client.request(method, path, headers=headers,
                                                 json={} if method == 'POST' else None)
            self.assertEqual(response.status, status)
        self.assertEqual(self.telegram_calls, [])

    async def test_test_fixture_rejects_non_loopback_outbound_before_socket(self):
        with self.assertRaisesRegex(AssertionError, 'block non-loopback'):
            await self.gateway.client.get('https://api.telegram.org/botinvalid/getMe')
        self.assertEqual(self.telegram_calls, [])

    async def test_telegram_token_validation_matches_core_ascii_and_length_bounds(self):
        for token in ('1:' + 'A' * 20, '9' * 20 + ':' + 'A' * 235):
            response = await self.client.get('/telegram/getMe', headers={
                'Authorization': 'Bearer synthetic-owner',
                'X-Habab-Telegram-Bot-Token': token,
            })
            self.assertEqual(response.status, 200)
        accepted = len(self.telegram_calls)
        for token in ('9' * 21 + ':' + 'A' * 20, '1:' + 'A' * 255,
                      '\u0661\u0662\u0663\u0664\u0665:' + 'A' * 35, '12345:' + 'A' * 19):
            response = await self.client.get('/telegram/getMe', headers={
                'Authorization': 'Bearer synthetic-owner',
                'X-Habab-Telegram-Bot-Token': token,
            })
            self.assertEqual(response.status, 400)
        self.assertEqual(len(self.telegram_calls), accepted)

    async def test_telegram_forwards_only_fixed_methods_and_preserves_botapi_status_body(self):
        token = '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk'
        headers = {'Authorization': 'Bearer synthetic-owner', 'X-Habab-Telegram-Bot-Token': token}
        response = await self.client.get('/telegram/getMe', headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {'ok': True, 'result': {'id': 12}})
        self.assertEqual(self.telegram_calls[-1], ('GET', True, 'getMe', b'', None, None, False))

        response = await self.client.post('/telegram/getMe', headers=headers, json={})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {'ok': True, 'result': {'id': 12}})
        self.assertEqual(self.telegram_calls[-1], ('POST', True, 'getMe', b'{}', None, None, False))

        response = await self.client.post('/telegram/getChatMember', headers=headers,
                                          json={'chat_id': 42, 'user_id': 7})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.telegram_calls[-1][1:3], (True, 'getChatMember'))

        response = await self.client.post('/telegram/sendChatAction', headers=headers,
                                          json={'chat_id': 42, 'action': 'typing'})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.telegram_calls[-1][1:3], (True, 'sendChatAction'))

        body = {'chat_id': 42, 'text': 'synthetic message'}
        self.telegram_payload = {'ok': False, 'error_code': 429,
                                 'parameters': {'retry_after': 7}, 'description': 'Too Many Requests'}
        self.telegram_status = 429
        self.telegram_retry_after = 7
        response = await self.client.post('/telegram/sendMessage', headers=headers, json=body)
        self.assertEqual(response.status, 429)
        self.assertEqual(response.headers.get('Retry-After'), '7')
        self.assertEqual(await response.json(), self.telegram_payload)
        self.assertEqual(self.telegram_calls[-1],
                         ('POST', True, 'sendMessage', json.dumps(body).encode(), None, None, False))
        self.assertEqual(len(self.telegram_calls), 5)
        self.assertEqual(self.calls, [])

    async def test_telegram_getupdates_is_one_manual_request_capped_at_25_seconds(self):
        token = '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk'
        headers = {'Authorization': 'Bearer synthetic-owner', 'X-Habab-Telegram-Bot-Token': token}
        self.gateway.upstream_key = None
        payload = {'offset': 101, 'timeout': 25, 'allowed_updates': ['message']}
        response = await self.client.post('/telegram/getUpdates', headers=headers, json=payload)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {'ok': True, 'result': {'id': 12}})
        self.assertEqual(len(self.telegram_calls), 1)
        self.assertEqual(self.telegram_calls[0][1:3], (True, 'getUpdates'))
        self.assertEqual(json.loads(self.telegram_calls[0][3]), payload)

        response = await self.client.post('/telegram/getUpdates', headers=headers,
                                          json={'timeout': 26})
        self.assertEqual(response.status, 400)
        self.assertEqual(len(self.telegram_calls), 1)

    async def test_telegram_body_and_upstream_response_limits_are_enforced(self):
        token = '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk'
        headers = {'Authorization': 'Bearer synthetic-owner', 'X-Habab-Telegram-Bot-Token': token}
        with patch('openai_gateway.TELEGRAM_REQUEST_MAX', 8):
            response = await self.client.post('/telegram/sendMessage', headers=headers, json={'text': 'too long'})
        self.assertEqual(response.status, 413)
        self.assertEqual(self.telegram_calls, [])

        self.telegram_large_response = True
        with patch('openai_gateway.TELEGRAM_RESPONSE_MAX', 16):
            response = await self.client.get('/telegram/getMe', headers=headers)
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())['error']['code'], 'telegram_response_too_large')
        self.assertEqual(len(self.telegram_calls), 1)

    async def test_telegram_bot_token_is_redacted_from_upstream_json_echo(self):
        token = '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk'
        headers = {'Authorization': 'Bearer synthetic-owner', 'X-Habab-Telegram-Bot-Token': token}
        self.telegram_payload = {'ok': False, 'description': 'token ' + token}
        self.telegram_status = 400
        response = await self.client.get('/telegram/getMe', headers=headers)
        payload = await response.read()
        self.assertEqual(response.status, 400)
        self.assertIn(b'[redacted]', payload)
        self.assertNotIn(token.encode(), payload)

    async def test_telegram_long_poll_timeout_is_sanitized(self):
        token = '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk'
        headers = {'Authorization': 'Bearer synthetic-owner', 'X-Habab-Telegram-Bot-Token': token}
        self.telegram_delay = 0.1
        with patch('openai_gateway.TELEGRAM_LONG_POLL_REQUEST_TIMEOUT', 0.02):
            response = await self.client.post('/telegram/getUpdates', headers=headers, json={'timeout': 25})
        self.assertEqual(response.status, 504)
        self.assertEqual((await response.json())['error']['code'], 'telegram_upstream_timeout')
        self.assertNotIn(token, await response.text())
        self.assertTrue(self.telegram_calls)

    async def test_telegram_downstream_cancellation_propagates_and_releases_http_slot(self):
        token = '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk'
        headers = {'Authorization': 'Bearer synthetic-owner', 'X-Habab-Telegram-Bot-Token': token}
        self.telegram_delay = 10
        request = asyncio.create_task(self.client.post('/telegram/getUpdates', headers=headers, json={'timeout': 25}))
        await asyncio.wait_for(self.telegram_started.wait(), timeout=2)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        await asyncio.wait_for(self.telegram_finished.wait(), timeout=2)
        self.assertEqual(self.gateway.inflight, 0)
        self.assertEqual(len(self.telegram_calls), 1)

    async def test_local_operator_enrolls_only_in_ram_and_socket_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'control.sock'
            gateway = Gateway(hashlib.sha256(b'synthetic-owner').hexdigest(), control_socket=path)
            async with TestClient(TestServer(gateway.app())) as client:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write(json.dumps({'upstream_key': 'synthetic-enrolled-vendor'}).encode() + b'\n')
                await writer.drain()
                self.assertEqual(json.loads(await reader.readline()), {'enrolled': True})
                writer.close()
                await writer.wait_closed()
                self.assertEqual(gateway.upstream_key, 'synthetic-enrolled-vendor')
                self.assertIsNone(gateway.text_upstream_key)
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write(json.dumps({'upstream_key': 'synthetic-text-vendor',
                                         'purpose': 'text'}).encode() + b'\n')
                await writer.drain()
                self.assertEqual(json.loads(await reader.readline()), {'enrolled': True})
                writer.close()
                await writer.wait_closed()
                self.assertEqual(gateway.upstream_key, 'synthetic-enrolled-vendor')
                self.assertEqual(gateway.text_upstream_key, 'synthetic-text-vendor')
                response = await client.post('/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent',
                    headers={'x-goog-api-key': 'synthetic-owner'}, json={'contents': []})
                self.assertEqual(response.status, 200)
                self.assertEqual(self.calls[-1][1], 'synthetic-enrolled-vendor')
                self.assertEqual(list(Path(directory).iterdir()), [path])
            self.assertIsNone(gateway.upstream_key)
            self.assertIsNone(gateway.text_upstream_key)
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
