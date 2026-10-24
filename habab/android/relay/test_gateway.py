"""Exercise auth, route boundaries and full response forwarding without live keys."""
import hashlib
import asyncio
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import WSMsgType, web
from aiohttp.client_exceptions import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from openai_gateway import Gateway


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.echo_secret = False
        self.large_response = False
        self.rotate_upstream_key = False
        self.oversized_token_response = False
        self.gemini_frames = []
        async def upstream(request):
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
            self.gemini_frames.append(('auth', request.headers.get('Authorization')))
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for message in ws:
                self.gemini_frames.append(('frame', message.data))
                if message.type == WSMsgType.BINARY:
                    await ws.send_bytes(message.data)
                else:
                    await ws.send_str(message.data)
            return ws
        app = web.Application()
        app.router.add_route('*', '/{path:.*}', upstream)
        app.router.add_get('/gemini', gemini_upstream)
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        self.gateway = Gateway(hashlib.sha256(b'synthetic-owner').hexdigest(), upstream_key='synthetic-vendor')
        self.patch = patch('openai_gateway.GEMINI_TEXT_BASE', str(self.upstream.make_url('/v1beta/models')).rstrip('/'))
        self.patch.start()
        self.gemini_patch = patch('openai_gateway.GEMINI_CONSTRAINED_UPSTREAM', str(self.upstream.make_url('/gemini')))
        self.gemini_patch.start()
        self.token_patch = patch('openai_gateway.GEMINI_TOKEN_URL', str(self.upstream.make_url('/v1beta/auth_tokens')))
        self.token_patch.start()
        self.client = TestClient(TestServer(self.gateway.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()
        self.patch.stop()
        self.gemini_patch.stop()
        self.token_patch.stop()

    async def bootstrap(self, setup=None, headers=None, path='/v1/realtime/bootstrap'):
        setup = setup or {'model': 'models/gemini-3.8-live', 'tools': [{'functionDeclarations': []}]}
        return await self.client.post(path, headers=headers or {'Authorization': 'Bearer synthetic-owner'},
                                      json={'model': 'gemini-3.8-live', 'setup': setup})

    async def test_capabilities_are_authenticated_and_explicit_about_gemini(self):
        response = await self.client.get('/v1/capabilities', headers={'Authorization': 'Bearer wrong'})
        self.assertEqual(response.status, 401)
        response = await self.client.get('/v1/capabilities', headers={'Authorization': 'Bearer synthetic-owner'})
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload['chat']['protocol'], 'google-generative-language-v1beta')
        self.assertEqual(payload['chat']['default_model'], 'gemini-3.1-flash-lite')
        self.assertEqual(payload['chat']['api_base'], 'https://habab-voice.wpmix.net/gemini/v1beta')
        self.assertEqual(payload['realtime']['transport'], 'habab')
        self.assertEqual(payload['realtime']['protocol'], 'google-generative-language-bidi-v1beta')
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

    async def test_legacy_openai_compatible_paths_reject_without_fallback(self):
        headers = {'Authorization': 'Bearer synthetic-owner'}
        response = await self.client.post('/v1/chat/completions', headers=headers, json={'model': 'gpt-4.1-mini'})
        self.assertEqual(response.status, 426)
        self.assertEqual((await response.json())['error']['code'], 'habab_api_upgrade_required')
        response = await self.client.post('/v1/realtime/client_secrets', headers=headers,
                                          json={'session': {'model': 'gpt-realtime-mini'}})
        self.assertEqual(response.status, 426)
        response = await self.client.get('/v1/realtime?model=gpt-realtime-mini', headers=headers)
        self.assertEqual(response.status, 404)
        self.assertEqual(self.calls, [])

    async def test_telegram_token_does_not_authorize_gemini_service(self):
        response = await self.client.get('/v1/capabilities', headers={'x-goog-api-key': 'synthetic-bot'})
        self.assertEqual(response.status, 401)
        response = await self.client.post('/telegram/deleteWebhook', headers={'Authorization': 'Bearer synthetic-bot'})
        self.assertEqual(response.status, 404)
        self.assertEqual(self.calls, [])

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
                response = await client.post('/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent',
                    headers={'x-goog-api-key': 'synthetic-owner'}, json={'contents': []})
                self.assertEqual(response.status, 200)
                self.assertEqual(self.calls[-1][1], 'synthetic-enrolled-vendor')
                self.assertEqual(list(Path(directory).iterdir()), [path])
            self.assertIsNone(gateway.upstream_key)
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
