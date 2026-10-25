"""Offline tests for the fixed-origin Exa MCP relay; no external sockets allowed."""
import asyncio
import hashlib
import io
import json
from multidict import CIMultiDict
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.client import ClientSession
from aiohttp.client_exceptions import ClientPayloadError
from aiohttp.test_utils import TestClient, TestServer

from exa_mcp_proxy import ExaMcpProxy, load_key_hash


class ExaMcpProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.blocked_outbound = []
        original_request = ClientSession._request

        async def guarded_request(session, method, url, **kwargs):
            host = urlsplit(str(url)).hostname
            if host not in {'127.0.0.1', 'localhost', '::1'}:
                self.blocked_outbound.append(host or 'invalid-host')
                raise AssertionError('MCP tests block non-loopback outbound requests')
            return await original_request(session, method, url, **kwargs)

        self.network_guard = patch.object(ClientSession, '_request', guarded_request)
        self.network_guard.start()
        self.records = []
        self.response_mode = 'json'
        self.upstream_status = 200
        self.response_payload = {'jsonrpc': '2.0', 'id': 1, 'result': {'tools': []}}
        self.upstream_delay = 0
        self.upstream_started = asyncio.Event()
        self.upstream_finished = asyncio.Event()
        self.response_size = 0

        async def upstream(request):
            body = await request.read()
            self.records.append({
                'method': request.method,
                'path': request.path,
                'query': request.query_string,
                'body': body,
                'authorization_present': request.headers.get('Authorization') is not None,
                'bearer_header_present': request.headers.get('X-Habab-Service-Key') is not None,
                'accept': request.headers.get('Accept'),
                'content_type': request.headers.get('Content-Type'),
                'protocol_version': request.headers.get('MCP-Protocol-Version'),
                'session_id': request.headers.get('Mcp-Session-Id'),
                'cookie': request.headers.get('Cookie'),
            })
            self.upstream_started.set()
            try:
                if self.upstream_delay:
                    await asyncio.sleep(self.upstream_delay)
                if self.response_mode == 'sse':
                    response = web.StreamResponse(status=self.upstream_status,
                                                   headers={'Content-Type': 'text/event-stream',
                                                            'Mcp-Session-Id': 'session-opaque-1'})
                    await response.prepare(request)
                    await response.write(b'event: message\ndata: {"jsonrpc":"2.0",')
                    await response.write(b'"id":1,"result":{"ok":true}}\n\n')
                    await response.write_eof()
                    return response
                if self.response_mode == 'sse-catalog':
                    response = web.StreamResponse(status=self.upstream_status,
                                                   headers={'Content-Type': 'text/event-stream'})
                    await response.prepare(request)
                    await response.write(b'event: message\ndata: ' + json.dumps(
                        self.response_payload, separators=(',', ':')).encode() + b'\n\n')
                    await response.write_eof()
                    return response
                if self.response_mode == 'sse-large':
                    response = web.StreamResponse(status=self.upstream_status,
                                                   headers={'Content-Type': 'text/event-stream'})
                    await response.prepare(request)
                    await response.write(b'data: ' + b'x' * 128 + b'\n\n')
                    await response.write_eof()
                    return response
                if self.response_mode == 'large':
                    return web.Response(body=b'x' * self.response_size, content_type='application/json')
                if self.upstream_status in (202, 204):
                    return web.Response(status=self.upstream_status)
                headers = {'Mcp-Session-Id': 'session-opaque-1',
                           'MCP-Protocol-Version': '2025-03-26',
                           'Set-Cookie': 'should-not-forward=1',
                           'WWW-Authenticate': 'Bearer private-upstream-challenge'}
                if self.upstream_status == 429:
                    headers['Retry-After'] = '9'
                return web.Response(body=json.dumps(self.response_payload, separators=(',', ':')).encode(),
                                    status=self.upstream_status, headers=headers,
                                    content_type='application/json')
            finally:
                self.upstream_finished.set()

        upstream_app = web.Application()
        upstream_app.router.add_post('/mcp', upstream)
        self.upstream = TestServer(upstream_app, handler_cancellation=True)
        await self.upstream.start_server()
        self.proxy = ExaMcpProxy(hashlib.sha256(b'synthetic-service-key').hexdigest())
        local_url = str(self.upstream.make_url('/mcp')) + '?tools=web_search_exa,get_code_context_exa'
        self.upstream_patch = patch('exa_mcp_proxy.EXA_MCP_URL', local_url)
        self.upstream_patch.start()
        self.client = TestClient(TestServer(self.proxy.app(), handler_cancellation=True))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()
        self.upstream_patch.stop()
        self.network_guard.stop()
        self.assertEqual(self.blocked_outbound, [])

    @property
    def auth(self):
        return {'Authorization': 'Bearer synthetic-service-key'}

    def rpc(self, method, params=None, request_id=1):
        payload = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
        if params is not None:
            payload['params'] = params
        return payload

    def rpc_headers(self, **extra):
        return {**self.auth, 'Accept': 'application/json, text/event-stream',
                'Content-Type': 'application/json', **extra}

    @staticmethod
    def tool_catalog():
        return {'jsonrpc': '2.0', 'id': 1, 'result': {'tools': [
            {'name': 'web_search_exa', 'description': 'Search web', 'inputSchema': {
                '$schema': 'http://json-schema.org/draft-07/schema#', 'type': 'object',
                'properties': {'query': {'type': 'string', 'minLength': 1},
                               'numResults': {'type': 'number'},
                               'objective': {'type': 'string', 'minLength': 1, 'maxLength': 4096}},
                'required': ['query', 'objective'], 'additionalProperties': False}},
            {'name': 'get_code_context_exa', 'description': 'Find code context', 'inputSchema': {
                '$schema': 'http://json-schema.org/draft-07/schema#', 'type': 'object',
                'properties': {'query': {'type': 'string', 'minLength': 1},
                               'numResults': {'type': 'number'}},
                'required': ['query'], 'additionalProperties': False}},
        ]}}

    async def post_rpc(self, payload, *, headers=None, path='/mcp/exa'):
        return await self.client.post(path, headers=headers or self.rpc_headers(), json=payload)

    async def test_health_and_capabilities_are_explicit_and_do_not_probe_upstream(self):
        health = await self.client.get('/health')
        self.assertEqual(health.status, 200)
        self.assertEqual(await health.json(), {'service': 'habab-exa-mcp-relay', 'ok': True})
        denied = await self.client.get('/mcp/exa/capabilities')
        self.assertEqual(denied.status, 401)
        response = await self.client.get('/mcp/exa/capabilities', headers=self.auth)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {
            'protocol': 'habab-exa-mcp-relay-v1', 'mcp_url': '/mcp/exa',
            'tools': ['web_search_exa', 'get_code_context_exa']})
        self.assertEqual(self.records, [])

    async def test_auth_duplicate_headers_and_bad_query_are_rejected_before_upstream(self):
        wrong = await self.post_rpc(self.rpc('ping'), headers=self.rpc_headers(Authorization='Bearer wrong'))
        self.assertEqual(wrong.status, 401)
        duplicate = CIMultiDict([('Authorization', 'Bearer synthetic-service-key'),
                                 ('Authorization', 'Bearer synthetic-service-key'),
                                 ('Accept', 'application/json, text/event-stream'),
                                 ('Content-Type', 'application/json')])
        duplicate_response = await self.post_rpc(self.rpc('ping'), headers=duplicate)
        self.assertEqual(duplicate_response.status, 401)
        query = await self.post_rpc(self.rpc('ping'), path='/mcp/exa?url=https://attacker.invalid')
        self.assertEqual(query.status, 400)
        self.assertEqual(self.records, [])

    async def test_invalid_json_size_methods_and_tools_never_reach_upstream(self):
        bad_json = await self.client.post('/mcp/exa', headers=self.rpc_headers(), data=b'{')
        self.assertEqual(bad_json.status, 400)
        oversized = await self.client.post('/mcp/exa', headers=self.rpc_headers(),
                                           data=io.BytesIO(b' ' * (1024 * 1024 + 1)))
        self.assertEqual(oversized.status, 413)
        get = await self.client.get('/mcp/exa', headers=self.auth)
        self.assertEqual(get.status, 405)
        delete = await self.client.delete('/mcp/exa', headers=self.auth)
        self.assertEqual(delete.status, 405)
        unknown_method = await self.post_rpc(self.rpc('resources/list'))
        self.assertEqual(unknown_method.status, 400)
        wrong_tool = await self.post_rpc(self.rpc('tools/call', {'name': 'web_fetch_exa', 'arguments': {'query': 'x'}}))
        self.assertEqual(wrong_tool.status, 400)
        invalid_query = await self.post_rpc(self.rpc('tools/call', {'name': 'web_search_exa',
                                                                     'arguments': {'query': 'x' * 4001,
                                                                                   'objective': 'find'}}))
        self.assertEqual(invalid_query.status, 400)
        missing_objective = await self.post_rpc(self.rpc('tools/call', {'name': 'web_search_exa',
                                                                         'arguments': {'query': 'x'}}))
        self.assertEqual(missing_objective.status, 400)
        bad_objective = await self.post_rpc(self.rpc('tools/call', {'name': 'web_search_exa',
                                                                     'arguments': {'query': 'x',
                                                                                   'objective': 'o' * 4097}}))
        self.assertEqual(bad_objective.status, 400)
        fractional_count = await self.post_rpc(self.rpc('tools/call', {'name': 'get_code_context_exa',
                                                                        'arguments': {'query': 'code',
                                                                                      'numResults': 1.5}}))
        self.assertEqual(fractional_count.status, 400)
        invalid_version = await self.post_rpc(self.rpc('ping'), headers=self.rpc_headers(
            **{'MCP-Protocol-Version': '2025-99-99'}))
        self.assertEqual(invalid_version.status, 400)
        self.assertEqual(self.records, [])
        self.assertEqual(self.proxy.inflight, 0)

    async def test_initialize_and_tool_requests_forward_exact_body_safe_headers_and_session(self):
        initialize = self.rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                              'clientInfo': {'name': 'synthetic-client', 'version': '1'}})
        body = json.dumps(initialize, separators=(',', ':')).encode()
        response = await self.client.post('/mcp/exa', headers=self.rpc_headers(
            **{'MCP-Protocol-Version': '2025-03-26'}), data=body)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), self.response_payload)
        self.assertEqual(response.headers.get('Mcp-Session-Id'), 'session-opaque-1')
        self.assertEqual(response.headers.get('MCP-Protocol-Version'), '2025-03-26')
        self.assertNotIn('Set-Cookie', response.headers)
        self.assertNotIn('WWW-Authenticate', response.headers)
        self.assertEqual(self.records[-1]['path'], '/mcp')
        self.assertEqual(self.records[-1]['body'], body)
        self.assertFalse(self.records[-1]['authorization_present'])
        self.assertFalse(self.records[-1]['bearer_header_present'])
        self.assertEqual(self.records[-1]['accept'], 'application/json, text/event-stream')
        self.assertEqual(self.records[-1]['content_type'], 'application/json')
        self.assertEqual(self.records[-1]['protocol_version'], '2025-03-26')
        self.assertEqual(self.records[-1]['query'], 'tools=web_search_exa,get_code_context_exa')

        call = self.rpc('tools/call', {'name': 'web_search_exa',
                                      'arguments': {'query': 'weather tomorrow', 'objective': 'weather forecast',
                                                    'numResults': 5}})
        call_body = json.dumps(call, separators=(',', ':')).encode()
        response = await self.client.post('/mcp/exa', headers=self.rpc_headers(
            **{'MCP-Protocol-Version': '2025-03-26', 'Mcp-Session-Id': 'session-opaque-1'}), data=call_body)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.records[-1]['body'], call_body)
        self.assertEqual(self.records[-1]['session_id'], 'session-opaque-1')
        self.assertFalse(self.records[-1]['authorization_present'])
        self.assertNotIn('synthetic-service-key', repr(self.records))

        code_call = self.rpc('tools/call', {'name': 'get_code_context_exa',
                                            'arguments': {'query': 'aiohttp timeout', 'numResults': 3}})
        code_body = json.dumps(code_call, separators=(',', ':')).encode()
        response = await self.client.post('/mcp/exa', headers=self.rpc_headers(), data=code_body)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.records[-1]['body'], code_body)
        self.assertIsNone(self.records[-1]['cookie'])

    async def test_tools_list_checks_vendor_schema_and_exposes_enforced_bounds(self):
        self.response_payload = self.tool_catalog()
        response = await self.post_rpc(self.rpc('tools/list'))
        self.assertEqual(response.status, 200)
        payload = await response.json()
        schemas = {tool['name']: tool['inputSchema'] for tool in payload['result']['tools']}
        self.assertEqual(set(schemas), {'web_search_exa', 'get_code_context_exa'})
        self.assertEqual(schemas['web_search_exa']['required'], ['query', 'objective'])
        self.assertEqual(schemas['web_search_exa']['properties']['query']['maxLength'], 4000)
        self.assertEqual(schemas['web_search_exa']['properties']['objective']['maxLength'], 4096)
        self.assertEqual(schemas['web_search_exa']['properties']['numResults'], {
            'type': 'integer', 'minimum': 1, 'maximum': 25})
        self.assertEqual(schemas['get_code_context_exa']['required'], ['query'])
        self.assertEqual(schemas['get_code_context_exa']['properties']['query']['maxLength'], 4000)
        self.assertEqual(schemas['get_code_context_exa']['properties']['numResults'], {
            'type': 'integer', 'minimum': 1, 'maximum': 25})

    async def test_tools_list_sse_is_bounded_and_schema_drift_is_rejected(self):
        self.response_mode = 'sse-catalog'
        self.response_payload = self.tool_catalog()
        response = await self.post_rpc(self.rpc('tools/list'))
        payload = await response.read()
        self.assertEqual(response.status, 200)
        self.assertIn(b'"maxLength":4000', payload)
        self.assertIn(b'"maximum":25', payload)

        self.response_mode = 'json'
        self.response_payload = self.tool_catalog()
        self.response_payload['result']['tools'][1]['inputSchema']['properties']['unexpected'] = {'type': 'string'}
        drift = await self.post_rpc(self.rpc('tools/list'))
        self.assertEqual(drift.status, 502)
        self.assertEqual((await drift.json())['error']['message'], 'Exa tool catalog is incompatible')

    async def test_initialized_notification_preserves_empty_202(self):
        self.upstream_status = 202
        notification = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
        response = await self.post_rpc(notification)
        self.assertEqual(response.status, 202)
        self.assertEqual(await response.read(), b'')
        self.assertEqual(json.loads(self.records[0]['body']), notification)

    async def test_cancelled_notification_is_narrowly_validated_and_forwarded(self):
        self.upstream_status = 202
        notification = {'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                        'params': {'requestId': 'request-7', 'reason': 'client cancelled'}}
        response = await self.post_rpc(notification)
        self.assertEqual(response.status, 202)
        self.assertEqual(json.loads(self.records[0]['body']), notification)
        malformed = await self.post_rpc({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                                         'params': {'requestId': 1, 'extra': True}})
        self.assertEqual(malformed.status, 400)
        self.assertEqual(len(self.records), 1)

    async def test_request_body_read_has_total_deadline(self):
        async def slow_body():
            yield b'{'
            await asyncio.sleep(0.2)
            yield b'}'

        with patch('exa_mcp_proxy.REQUEST_BODY_TIMEOUT_SECONDS', 0.02):
            response = await self.client.post('/mcp/exa', headers=self.rpc_headers(), data=slow_body())
        self.assertEqual(response.status, 408)
        self.assertEqual((await response.json())['error']['message'], 'Request body timed out')
        self.assertEqual(self.records, [])

    async def test_sse_response_streams_byte_for_byte(self):
        self.response_mode = 'sse'
        response = await self.post_rpc(self.rpc('ping'))
        payload = await response.read()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get('Content-Type'), 'text/event-stream')
        self.assertEqual(payload, b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n')
        self.assertEqual(self.proxy.inflight, 0)

    async def test_http_429_is_sanitized_and_retries_header_is_filtered(self):
        self.upstream_status = 429
        self.response_payload = {'error': {'message': 'private upstream text'}}
        with patch('exa_mcp_proxy._LOGGER.warning') as log_failure:
            response = await self.post_rpc(self.rpc('tools/call', {'name': 'web_search_exa',
                                                                   'arguments': {'query': 'private-query-sentinel',
                                                                                 'objective': 'private-objective-sentinel'}}),
                                          headers=self.rpc_headers(**{'Mcp-Session-Id': 'private-session-sentinel'}))
        self.assertEqual(response.status, 429)
        self.assertEqual(response.headers.get('Retry-After'), '9')
        self.assertNotIn('WWW-Authenticate', response.headers)
        payload = await response.json()
        self.assertEqual(payload['error']['message'], 'Exa upstream request failed')
        self.assertNotIn('private upstream text', json.dumps(payload))
        self.assertEqual(log_failure.call_count, 1)
        log_values = log_failure.call_args.args
        self.assertEqual(log_values[1:4], ('upstream_http_error', 429, 'web_search_exa'))
        self.assertEqual(log_values[4], 1)
        self.assertIsInstance(log_values[5], int)
        self.assertEqual(log_values[6], 9)
        self.assertNotIn('private-query-sentinel', repr(log_values))
        self.assertNotIn('private-objective-sentinel', repr(log_values))
        self.assertNotIn('private-session-sentinel', repr(log_values))
        self.assertNotIn('synthetic-service-key', repr(log_values))
        self.assertNotIn('private upstream text', repr(log_values))

    async def test_upstream_503_logs_safe_origin_category_and_duration(self):
        self.upstream_status = 503
        self.response_payload = {'error': {'message': 'private upstream text'}}
        with patch('exa_mcp_proxy._LOGGER.warning') as log_failure:
            response = await self.post_rpc(self.rpc('tools/call', {'name': 'web_search_exa',
                                                                   'arguments': {'query': 'private-query-sentinel',
                                                                                 'objective': 'private-objective-sentinel'}}))
        self.assertEqual(response.status, 503)
        self.assertEqual(log_failure.call_count, 1)
        log_values = log_failure.call_args.args
        self.assertEqual(log_values[0],
                         'exa_relay_failure category=%s http_status=%d tool=%s inflight=%d duration_ms=%d retry_after=%s')
        self.assertEqual(len(log_values), 7)
        self.assertEqual(log_values[1:4], ('upstream_http_error', 503, 'web_search_exa'))
        self.assertEqual(log_values[4], 1)
        self.assertIsInstance(log_values[5], int)
        self.assertEqual(log_values[6], '-')
        for private_value in ('private-query-sentinel', 'private-objective-sentinel',
                              'synthetic-service-key', 'private upstream text'):
            self.assertNotIn(private_value, repr(log_values))

    async def test_local_capacity_429_logs_distinct_safe_category(self):
        self.proxy.inflight = 4
        try:
            with patch('exa_mcp_proxy._LOGGER.warning') as log_failure:
                response = await self.post_rpc(self.rpc('tools/call', {'name': 'web_search_exa',
                                                                       'arguments': {'query': 'private-query-sentinel',
                                                                                     'objective': 'private-objective-sentinel'}}))
            self.assertEqual(response.status, 429)
            self.assertEqual(self.records, [])
            self.assertEqual(log_failure.call_count, 1)
            log_values = log_failure.call_args.args
            self.assertEqual(log_values[1:4], ('local_inflight_limit', 429, 'web_search_exa'))
            self.assertEqual(log_values[4], 4)
            self.assertIsInstance(log_values[5], int)
            self.assertEqual(log_values[6], '-')
            self.assertNotIn('private-query-sentinel', repr(log_values))
            self.assertNotIn('private-objective-sentinel', repr(log_values))
        finally:
            self.proxy.inflight = 0

    async def test_upstream_redirect_is_not_followed_and_error_is_sanitized(self):
        self.upstream_status = 302
        response = await self.post_rpc(self.rpc('ping'))
        self.assertEqual(response.status, 502)
        self.assertEqual(len(self.records), 1)
        self.assertEqual((await response.json())['error']['message'], 'Exa upstream redirect rejected')

    async def test_upstream_body_limit_rejects_oversized_json(self):
        self.response_mode = 'large'
        self.response_size = 128
        with patch('exa_mcp_proxy.MAX_RESPONSE_BYTES', 16):
            response = await self.post_rpc(self.rpc('ping'))
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())['error']['message'], 'Exa upstream response is too large')
        self.assertEqual(self.proxy.inflight, 0)

    async def test_sse_response_limit_closes_stream_without_reporting_completion(self):
        self.response_mode = 'sse-large'
        with patch('exa_mcp_proxy.MAX_RESPONSE_BYTES', 16):
            response = await self.post_rpc(self.rpc('ping'))
            with self.assertRaises(ClientPayloadError):
                await response.read()
        self.assertEqual(self.proxy.inflight, 0)

    async def test_timeout_is_bounded_and_sanitized(self):
        self.upstream_delay = 0.1
        with patch('exa_mcp_proxy.REQUEST_TIMEOUT_SECONDS', 0.02):
            response = await self.post_rpc(self.rpc('tools/call', {'name': 'web_search_exa',
                                                                   'arguments': {'query': 'x',
                                                                                 'objective': 'test'}}))
        self.assertEqual(response.status, 504)
        self.assertEqual((await response.json())['error']['message'], 'Exa upstream timed out')
        self.assertEqual(self.proxy.inflight, 0)

    async def test_downstream_cancel_closes_upstream_and_releases_slot(self):
        self.upstream_delay = 10
        task = asyncio.create_task(self.post_rpc(self.rpc('tools/call', {'name': 'web_search_exa',
                                                                          'arguments': {'query': 'x',
                                                                                        'objective': 'test'}})))
        await asyncio.wait_for(self.upstream_started.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(self.upstream_finished.wait(), timeout=2)
        self.assertEqual(self.proxy.inflight, 0)
        self.assertEqual(len(self.records), 1)

    async def test_private_config_requires_owner_directory_and_0600_file(self):
        with tempfile.TemporaryDirectory() as directory:
            os_dir = Path(directory)
            os_dir.chmod(0o700)
            config_path = os_dir / 'auth.json'
            expected = hashlib.sha256(b'synthetic-service-key').hexdigest()
            config_path.write_text(json.dumps({'key_sha256': expected}), encoding='utf-8')
            config_path.chmod(0o600)
            self.assertEqual(load_key_hash(config_path), expected)
            config_path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'private_config_file_required'):
                load_key_hash(config_path)

    async def test_test_network_guard_rejects_non_loopback_before_socket(self):
        with self.assertRaisesRegex(AssertionError, 'block non-loopback'):
            await self.proxy.client.get('https://mcp.exa.ai/mcp')
        self.assertEqual(self.blocked_outbound, ['mcp.exa.ai'])
        self.blocked_outbound.clear()
        self.assertEqual(self.records, [])


if __name__ == '__main__':
    unittest.main()
