"""Managed Responses transport with fixture credentials and loopback providers only."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from beta_codes import BetaCodes, create_config
from openai_gateway import CODEX_RESPONSES_URL, Gateway, load_private_config
from provider_health import ProviderHealth
from usage_stats import UsageJournal


class CodexGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.owner = 'fixture-habab-installation'
        self.vendor = 'fixture-codex-openai-key'
        self.headers = {'Authorization': 'Bearer ' + self.owner}
        self.calls = []
        self.status = 200
        self.stream = False
        self.stall = False
        self.echo = False
        self.wrong_content_type = False
        self.request_id = 'req_fixture'
        self.upstream_disconnected = asyncio.Event()
        self.reply = {'id': 'resp_fixture123', 'object': 'response', 'status': 'completed',
                      'model': 'gpt-6-luna', 'service_tier': 'priority', 'output': [],
                      'usage': {'input_tokens': 12, 'output_tokens': 3, 'total_tokens': 15}}
        self.events = (b'event: response.output_item.added\ndata: {"type":"response.output_item.added",'
                       b'"item":{"type":"function_call","call_id":"call_fixture","name":"ha",'
                       b'"arguments":"{}"}}\n\n' + b'event: response.completed\ndata: ' +
                       json.dumps({'type': 'response.completed', 'response': self.reply}).encode() + b'\n\n')

        async def upstream(request):
            self.calls.append((str(request.url), dict(request.headers), await request.json()))
            if self.wrong_content_type:
                return web.Response(text='not SSE')
            if self.stall:
                response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
                await response.prepare(request)
                await response.write(b'data: {"type":"response.created"}\n\n' + b': padding ' + b'x' * 80 + b'\n\n')
                try:
                    for _ in range(200):
                        if request.transport is None or request.transport.is_closing():
                            self.upstream_disconnected.set()
                            return response
                        await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    self.upstream_disconnected.set()
                    raise
                return response
            if self.stream:
                response = web.StreamResponse(status=self.status, headers={'Content-Type': 'text/event-stream', 'x-request-id': 'req_fixture'})
                await response.prepare(request)
                data = self.events
                if self.echo:
                    data = b'data: ' + json.dumps({'key': self.vendor, 'install': self.owner}).encode() + b'\n\n' + data
                for offset in range(0, len(data), 7):
                    await response.write(data[offset:offset + 7])
                await response.write_eof()
                return response
            payload = self.reply if self.status == 200 else {'error': {'code': 'rate_limit_exceeded', 'message': self.vendor + ' ' + self.owner}}
            return web.json_response(payload, status=self.status, headers={'x-request-id': self.request_id})

        app = web.Application()
        app.router.add_post('/v1/responses', upstream)
        self.provider = TestServer(app)
        await self.provider.start_server()
        self.addAsyncCleanup(self.provider.close)
        original_request = ClientSession._request

        async def guarded_request(session, method, url, **kwargs):
            if str(url) == CODEX_RESPONSES_URL:
                self.assertEqual(method, 'POST')
                self.assertIs(kwargs.get('allow_redirects'), False)
                url = self.provider.make_url('/v1/responses')
            elif urlsplit(str(url)).hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise AssertionError('Tests prohibit external providers')
            return await original_request(session, method, url, **kwargs)

        guard = patch.object(ClientSession, '_request', guarded_request)
        guard.start()
        self.addCleanup(guard.stop)
        beta_path = self.root / 'beta' / 'config.json'
        create_config(beta_path)
        self.gateway = Gateway(hashlib.sha256(self.owner.encode()).hexdigest(),
                               codex_upstream_key=self.vendor, beta_codes=BetaCodes(beta_path),
                               usage_journal=UsageJournal(self.root / 'usage'))
        self.gateway.codex_health = ProviderHealth('openai', 'gpt-6-luna', self.root / 'codex-health.json')
        self.client = TestClient(TestServer(self.gateway.app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def post(self, **overrides):
        payload = {'model': 'gpt-6-luna', 'input': 'fixture task', **overrides}
        return await self.client.post('/codex/v1/responses', headers=self.headers, json=payload)

    def journal(self):
        return [json.loads(line) for path in (self.root / 'usage').glob('*.jsonl') for line in path.read_text().splitlines()]

    async def test_fixed_upstream_preserves_codex_body_and_forces_fast(self):
        payload = {'model': 'gpt-6-luna', 'input': [{'type': 'function_call_output', 'call_id': 'call_1', 'output': '{}'}],
                   'instructions': 'fixture', 'reasoning': {'effort': 'low', 'summary': 'auto'},
                   'tools': [{'type': 'function', 'name': 'ha', 'strict': True, 'parameters': {'type': 'object',
                              'properties': {'id': {'type': 'string'}}, 'required': ['id'], 'additionalProperties': False}}],
                   'text': {'format': {'type': 'json_schema', 'name': 'result', 'schema': {'type': 'object'}}},
                   'store': False, 'service_tier': 'fast'}
        response = await self.post(**payload)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), self.reply)
        sent = self.calls[0][2]
        self.assertEqual(sent, {**payload, 'service_tier': 'priority'})
        self.assertEqual(self.calls[0][1]['Authorization'], 'Bearer ' + self.vendor)
        self.assertNotIn(self.owner, json.dumps(self.calls))
        record = self.journal()[0]
        self.assertEqual(record['usage'], {'prompt_tokens': 12, 'completion_tokens': 3, 'total_tokens': 15})
        self.assertEqual((record['response_id'], record['request_id'], record['service_tier']),
                         ('resp_fixture123', 'req_fixture', 'priority'))
        self.assertEqual(record['channel'], 'codex')
        self.assertNotIn('fixture task', json.dumps(record))

    async def test_auth_methods_queries_and_routes_rejected_before_provider(self):
        for headers in ({}, {'Authorization': 'Bearer wrong'}, {'Authorization': 'Bearer ' + self.vendor},
                        {**self.headers, 'x-goog-api-key': self.owner}):
            response = await self.client.post('/codex/v1/responses', headers=headers, json={})
            self.assertEqual(response.status, 401)
        response = await self.client.get('/codex/v1/responses', headers=self.headers)
        self.assertEqual(response.status, 405)
        response = await self.client.post('/codex/v1/responses?upstream=https://invalid', headers=self.headers, json={})
        self.assertEqual(response.status, 400)
        for route in ('/codex/v1/models', '/codex/v1/responses/resp_other', '/codex/v1/chat/completions'):
            response = await self.client.post(route, headers=self.headers, json={})
            self.assertEqual(response.status, 404)
        self.assertEqual(self.calls, [])

    async def test_beta_installation_key_is_accepted(self):
        import uuid
        enrolled = await self.client.post('/beta/enroll', json={'installation_id': str(uuid.uuid4())})
        code = (await enrolled.json())['code']
        self.headers = {'Authorization': 'Bearer ' + code}
        response = await self.post()
        self.assertEqual(response.status, 200)
        self.assertEqual(self.journal()[0]['installation'], hashlib.sha256(code.encode()).hexdigest())

    async def test_bounds_model_secrets_duplicate_fields_and_concurrency(self):
        for overrides, expected in (({'model': 'gpt-6-astra'}, 404), ({'stream': 'true'}, 400),
                                    ({'input': {'bad': True}}, 400), ({'input': self.owner}, 400),
                                    ({'input': self.vendor}, 400), ({'service_tier': []}, 400)):
            response = await self.post(**overrides)
            self.assertEqual(response.status, expected)
        response = await self.client.post('/codex/v1/responses', headers={**self.headers, 'Content-Type': 'application/json'},
                                          data=b'{"model":"gpt-6-luna","input":"a","input":"b"}')
        self.assertEqual(response.status, 400)
        response = await self.post(input='x' * (2 * 1024 * 1024))
        self.assertEqual(response.status, 413)
        async def chunks():
            for _ in range(33):
                yield b'x' * 65536
        response = await self.client.post('/codex/v1/responses', headers={**self.headers, 'Content-Type': 'application/json'}, data=chunks())
        self.assertEqual(response.status, 413)
        self.gateway.inflight = 6
        response = await self.post()
        self.assertEqual(response.status, 429)
        self.gateway.inflight = 0
        self.gateway.codex_upstream_key = None
        response = await self.post()
        self.assertEqual(response.status, 503)
        self.assertEqual(self.calls, [])

    async def test_stream_preserves_responses_events_tools_usage_and_redacts_split_credentials(self):
        self.stream = True
        response = await self.post(stream=True, service_tier='priority')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['X-Accel-Buffering'], 'no')
        self.assertEqual(await response.read(), self.events)
        await asyncio.sleep(0.01)
        self.assertEqual(self.journal()[0]['status'], 'ok')
        self.assertEqual(self.journal()[0]['usage']['total_tokens'], 15)
        self.echo = True
        response = await self.post(stream=True)
        wire = await response.read()
        self.assertNotIn(self.vendor.encode(), wire)
        self.assertNotIn(self.owner.encode(), wire)
        self.assertIn(b'[redacted]', wire)

    async def test_provider_error_is_redacted_and_health_observed(self):
        self.status = 429
        response = await self.post()
        self.assertEqual(response.status, 429)
        body = await response.text()
        self.assertIn('rate_limit_exceeded', body)
        self.assertNotIn(self.vendor, body)
        self.assertNotIn(self.owner, body)
        self.assertEqual(self.gateway.codex_health.snapshot()['reason'], 'rate_limited')
        self.assertEqual(self.journal()[0]['status'], 'failed')

    async def test_stalled_provider_closes_promptly_when_client_disconnects(self):
        self.stall = True
        response = await self.post(stream=True)
        await response.content.read(32)
        response.close()
        await asyncio.sleep(0.3)
        self.assertEqual(self.gateway.inflight, 0, self.journal())
        await asyncio.wait_for(self.upstream_disconnected.wait(), 1.5)
        self.assertEqual(self.gateway.inflight, 0)
        self.assertEqual(self.journal()[0]['status'], 'cancelled')

    async def test_invalid_stream_content_is_rejected(self):
        self.wrong_content_type = True
        response = await self.post(stream=True)
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())['error']['code'], 'relay_invalid_response')

    async def test_response_limit_timeout_redirect_and_error_events(self):
        with patch('openai_gateway.CODEX_RESPONSE_MAX', 64):
            response = await self.post()
            self.assertEqual(response.status, 502)
            self.assertEqual((await response.json())['error']['code'], 'relay_response_too_large')
        with patch.object(self.gateway.client, 'post', side_effect=asyncio.TimeoutError):
            response = await self.post()
            self.assertEqual(response.status, 504)
        self.status = 302
        response = await self.post()
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())['error']['code'], 'relay_upstream_redirect_rejected')
        self.status = 200
        self.stream = True
        self.events = b'event: response.failed\ndata: {"type":"response.failed","response":{"id":"resp_failed","status":"failed","error":{"code":"server_error"}}}\n\n'
        response = await self.post(stream=True)
        self.assertEqual(await response.read(), self.events)
        await asyncio.sleep(0.01)
        self.assertEqual(self.journal()[-1]['status'], 'failed')
        self.assertEqual(self.gateway.codex_health.snapshot()['reason'], 'provider_unavailable')

    async def test_credential_echo_in_provider_request_id_is_not_journaled(self):
        self.request_id = self.vendor
        response = await self.post()
        await response.read()
        self.assertNotIn('request_id', self.journal()[0])
        self.assertNotIn(self.vendor, json.dumps(self.journal()))

    async def test_readiness_distinguishes_configured_from_provider_proof(self):
        response = await self.client.get('/health/ready')
        payload = await response.json()
        self.assertTrue(payload['codex']['configured'])
        self.assertFalse(payload['codex']['ready'])
        await self.post()
        response = await self.client.get('/health/ready')
        self.assertTrue((await response.json())['codex']['ready'])

    def test_private_config_accepts_separate_codex_key(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps({'key_sha256': 'a' * 64, 'upstream_keys': {'codex': self.vendor, 'voice': 'fixture-voice'}}))
        path.chmod(0o600)
        self.assertEqual(load_private_config(path)['upstream_keys']['codex'], self.vendor)
