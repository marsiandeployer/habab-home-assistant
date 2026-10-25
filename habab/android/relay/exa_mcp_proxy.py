"""Narrow authenticated Streamable HTTP proxy for approved Exa MCP tools."""
import argparse
import asyncio
from datetime import date
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import time

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, web

EXA_MCP_URL = 'https://mcp.exa.ai/mcp?tools=web_search_exa,get_code_context_exa'
SERVICE = 'habab-exa-mcp-relay'
PROTOCOL = 'habab-exa-mcp-relay-v1'
TOOL_NAMES = ('web_search_exa', 'get_code_context_exa')
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_INFLIGHT = 4
CONNECT_TIMEOUT_SECONDS = 10
REQUEST_TIMEOUT_SECONDS = 60
REQUEST_BODY_TIMEOUT_SECONDS = 10
QUERY_MAX_CHARS = 4000
OBJECTIVE_MAX_CHARS = 4096
NUM_RESULTS_MAX = 25
MCP_PROTOCOL_VERSION = re.compile(r'\d{4}-\d{2}-\d{2}\Z')
_LOGGER = logging.getLogger(__name__)

TOOL_REQUIREMENTS = {
    'web_search_exa': {'required': {'query', 'objective'}, 'properties': {'query', 'objective', 'numResults'}},
    'get_code_context_exa': {'required': {'query'}, 'properties': {'query', 'numResults'}},
}


def jsonrpc_error(request_id, code, message, *, status=400, headers=None):
    return web.json_response({'jsonrpc': '2.0', 'id': request_id,
                              'error': {'code': code, 'message': message}},
                             status=status, headers=headers)


def _failure_tool_name(payload):
    params = payload.get('params') if isinstance(payload, dict) else None
    name = params.get('name') if isinstance(params, dict) else None
    return name if name in TOOL_NAMES else 'none'


def _log_failure(category, status, tool, inflight, started_at, retry_after=None):
    if not isinstance(retry_after, int) or isinstance(retry_after, bool) or retry_after < 0:
        retry_after = '-'
    _LOGGER.warning(
        'exa_relay_failure category=%s http_status=%d tool=%s inflight=%d duration_ms=%d retry_after=%s',
        category, status, tool, inflight, max(0, int((time.monotonic() - started_at) * 1000)),
        retry_after,
    )


def _object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def _valid_mcp_header(value, limit=256):
    return (isinstance(value, str) and 1 <= len(value) <= limit
            and all(0x21 <= ord(char) <= 0x7e for char in value))


def _valid_protocol_version(value):
    if not isinstance(value, str) or len(value) > 32 or MCP_PROTOCOL_VERSION.fullmatch(value) is None:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _valid_jsonrpc_id(value):
    return (isinstance(value, (str, int, float)) and not isinstance(value, bool)
            and not (isinstance(value, float) and not math.isfinite(value)))


def _bounded_tool_catalog(payload):
    """Validate Exa's expected tool schemas, then expose our enforced bounds."""
    if not isinstance(payload, dict) or not isinstance(payload.get('result'), dict):
        raise ValueError('invalid_tool_catalog')
    tools = payload['result'].get('tools')
    if not isinstance(tools, list) or len(tools) != len(TOOL_NAMES):
        raise ValueError('invalid_tool_catalog')
    by_name = {}
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get('name'), str):
            raise ValueError('invalid_tool_catalog')
        name = tool['name']
        if name not in TOOL_REQUIREMENTS or name in by_name:
            raise ValueError('invalid_tool_catalog')
        schema = tool.get('inputSchema')
        requirements = TOOL_REQUIREMENTS[name]
        required = schema.get('required') if isinstance(schema, dict) else None
        if (not isinstance(schema, dict) or schema.get('type') != 'object'
                or schema.get('additionalProperties') is not False
                or not isinstance(required, list) or not all(isinstance(item, str) for item in required)
                or set(required) != requirements['required']
                or not isinstance(schema.get('properties'), dict)
                or set(schema['properties']) != requirements['properties']):
            raise ValueError('invalid_tool_catalog')
        for prop in requirements['properties']:
            definition = schema['properties'].get(prop)
            expected_type = 'number' if prop == 'numResults' else 'string'
            if not isinstance(definition, dict) or definition.get('type') != expected_type:
                raise ValueError('invalid_tool_catalog')
            if prop == 'query' and definition.get('minLength') != 1:
                raise ValueError('invalid_tool_catalog')
            if prop == 'objective' and (definition.get('minLength') != 1
                                        or definition.get('maxLength') != OBJECTIVE_MAX_CHARS):
                raise ValueError('invalid_tool_catalog')
        by_name[name] = tool
    if set(by_name) != set(TOOL_NAMES):
        raise ValueError('invalid_tool_catalog')
    for name, tool in by_name.items():
        properties = tool['inputSchema']['properties']
        properties['query']['maxLength'] = QUERY_MAX_CHARS
        if name == 'web_search_exa':
            properties['objective']['maxLength'] = OBJECTIVE_MAX_CHARS
        if 'numResults' in properties:
            properties['numResults'].update({'type': 'integer', 'minimum': 1, 'maximum': NUM_RESULTS_MAX})
    payload['result']['tools'] = [by_name[name] for name in TOOL_NAMES]
    return payload


def _rewrite_tool_catalog_sse(body):
    """Validate and bound the finite SSE tools/list response without losing framing."""
    separator = re.compile(rb'(\r?\n\r?\n)')
    pieces = separator.split(body)
    found = False
    for index in range(0, len(pieces), 2):
        frame = pieces[index]
        lines = frame.splitlines(keepends=True)
        data_indexes = [i for i, line in enumerate(lines) if line.startswith(b'data:')]
        if not data_indexes:
            continue
        data_value = b'\n'.join(lines[i][5:].lstrip(b' ').rstrip(b'\r\n') for i in data_indexes)
        try:
            event = json.loads(data_value)
        except (ValueError, UnicodeError, TypeError):
            continue
        if not (isinstance(event, dict) and isinstance(event.get('result'), dict)
                and 'tools' in event['result']):
            continue
        if found:
            raise ValueError('duplicate_tool_catalog')
        try:
            event = _bounded_tool_catalog(event)
        except ValueError as error:
            raise ValueError('invalid_tool_catalog') from error
        replacement = b'data: ' + json.dumps(event, separators=(',', ':'), ensure_ascii=False).encode('utf-8') + b'\n'
        if data_indexes:
            first = data_indexes[0]
            ending = b'\r\n' if lines[first].endswith(b'\r\n') else b'\n'
            lines[first] = replacement[:-1] + ending
            for extra in reversed(data_indexes[1:]):
                del lines[extra]
        pieces[index] = b''.join(lines)
        found = True
    if not found:
        raise ValueError('missing_tool_catalog')
    return b''.join(pieces)


def validate_rpc(payload):
    """Return a safe JSON-RPC request id, or a sanitized validation error."""
    if not isinstance(payload, dict) or set(payload) - {'jsonrpc', 'id', 'method', 'params'}:
        return None, (-32600, 'Invalid JSON-RPC request')
    if payload.get('jsonrpc') != '2.0' or not isinstance(payload.get('method'), str):
        return None, (-32600, 'Invalid JSON-RPC request')
    method = payload['method']
    if method in {'notifications/initialized', 'notifications/cancelled'}:
        params = payload.get('params', {})
        if 'id' in payload or not isinstance(params, dict):
            return None, (-32600, 'Invalid JSON-RPC notification')
        if method == 'notifications/initialized' and params:
            return None, (-32600, 'Invalid JSON-RPC notification')
        if method == 'notifications/cancelled':
            if (set(params) - {'requestId', 'reason'} or not _valid_jsonrpc_id(params.get('requestId'))
                    or ('reason' in params and (not isinstance(params['reason'], str)
                                                 or len(params['reason']) > 256))):
                return None, (-32600, 'Invalid cancellation notification')
        return None, None

    request_id = payload.get('id')
    if not _valid_jsonrpc_id(request_id):
        return None, (-32600, 'Invalid JSON-RPC request id')
    params = payload.get('params', {})
    if not isinstance(params, dict):
        return request_id, (-32602, 'Invalid method parameters')

    if method not in {'initialize', 'ping', 'tools/list', 'tools/call'}:
        return request_id, (-32601, 'Method not allowed')
    if method == 'initialize' and not params:
        return request_id, (-32602, 'Invalid initialization parameters')
    if method == 'tools/call':
        if set(params) - {'name', 'arguments', '_meta'} or params.get('name') not in TOOL_NAMES:
            return request_id, (-32602, 'Tool not allowed')
        arguments = params.get('arguments')
        allowed_arguments = TOOL_REQUIREMENTS[params['name']]['properties']
        if not isinstance(arguments, dict) or set(arguments) - allowed_arguments:
            return request_id, (-32602, 'Invalid tool arguments')
        query = arguments.get('query')
        if not isinstance(query, str) or not query.strip() or len(query) > QUERY_MAX_CHARS:
            return request_id, (-32602, 'Invalid search query')
        if params['name'] == 'web_search_exa':
            objective = arguments.get('objective')
            if not isinstance(objective, str) or not objective.strip() or len(objective) > OBJECTIVE_MAX_CHARS:
                return request_id, (-32602, 'Invalid search objective')
        if 'numResults' in arguments:
            count = arguments['numResults']
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= NUM_RESULTS_MAX:
                return request_id, (-32602, 'Invalid result count')
        if '_meta' in params and not isinstance(params['_meta'], dict):
            return request_id, (-32602, 'Invalid method metadata')
    return request_id, None


def load_key_hash(config_path):
    path = Path(config_path)
    try:
        parent_info = path.parent.lstat()
        file_info = path.lstat()
    except OSError as error:
        raise ValueError('private_config_unavailable') from error
    if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) != 0o700):
        raise ValueError('private_config_directory_required')
    if (not stat.S_ISREG(file_info.st_mode) or stat.S_ISLNK(file_info.st_mode)
            or file_info.st_uid != os.getuid() or stat.S_IMODE(file_info.st_mode) != 0o600
            or file_info.st_size > 64 * 1024):
        raise ValueError('private_config_file_required')
    try:
        config = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_object_without_duplicate_keys)
        key_hash = config.get('key_sha256') if isinstance(config, dict) else None
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise ValueError('private_config_invalid') from error
    if not isinstance(key_hash, str) or re.fullmatch(r'[0-9a-f]{64}', key_hash) is None:
        raise ValueError('private_config_invalid')
    return key_hash


class ExaMcpProxy:
    def __init__(self, key_hash):
        if not isinstance(key_hash, str) or re.fullmatch(r'[0-9a-f]{64}', key_hash) is None:
            raise ValueError('invalid_service_key_hash')
        self.key_hash = key_hash
        self.client = None
        self.inflight = 0

    def authorized(self, request):
        values = request.headers.getall('Authorization', [])
        if len(values) != 1:
            return False
        value = values[0]
        if not value.startswith('Bearer ') or len(value) > 2048:
            return False
        secret = value[7:]
        if not secret or any(ord(char) < 33 or ord(char) > 126 for char in secret):
            return False
        return hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(), self.key_hash)

    async def lifecycle(self, app):
        self.client = ClientSession(timeout=ClientTimeout(total=REQUEST_TIMEOUT_SECONDS), trust_env=False,
                                    cookie_jar=DummyCookieJar())
        try:
            yield
        finally:
            await self.client.close()
            self.client = None

    async def health(self, request):
        return web.json_response({'service': SERVICE, 'ok': True})

    async def capabilities(self, request):
        if not self.authorized(request):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        if request.query:
            return web.json_response({'error': {'code': 'relay_invalid_query'}}, status=400)
        return web.json_response({'protocol': PROTOCOL, 'mcp_url': '/mcp/exa', 'tools': list(TOOL_NAMES)},
                                 headers={'Cache-Control': 'no-store'})

    async def handle(self, request):
        started_at = time.monotonic()
        if not self.authorized(request):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        if request.query:
            return jsonrpc_error(None, -32600, 'Query parameters are not allowed')
        if request.headers.get('Origin'):
            return web.json_response({'error': {'code': 'relay_origin_not_allowed'}}, status=403)
        if request.method != 'POST':
            return web.Response(status=405, headers={'Allow': 'POST'})
        if request.content_type != 'application/json':
            return jsonrpc_error(None, -32600, 'Content-Type must be application/json')
        accepts = request.headers.getall('Accept', [])
        accept = ','.join(accepts)
        if len(accept) > 512:
            return jsonrpc_error(None, -32600, 'Unsupported Accept header')
        media_types = {part.split(';', 1)[0].strip().lower() for part in accept.split(',')}
        if not {'application/json', 'text/event-stream'} <= media_types:
            return web.json_response({'error': {'code': 'relay_invalid_accept'}}, status=406)
        if request.content_length is not None and request.content_length > MAX_REQUEST_BYTES:
            return jsonrpc_error(None, -32600, 'Request body is too large', status=413)
        async def read_bounded_body():
            body = bytearray()
            async for block in request.content.iter_chunked(65536):
                body.extend(block)
                if len(body) > MAX_REQUEST_BYTES:
                    raise OverflowError
            return bytes(body)

        try:
            body = await asyncio.wait_for(read_bounded_body(), timeout=REQUEST_BODY_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return jsonrpc_error(None, -32000, 'Request body timed out', status=408)
        except OverflowError:
            return jsonrpc_error(None, -32600, 'Request body is too large', status=413)
        try:
            payload = json.loads(bytes(body), object_pairs_hook=_object_without_duplicate_keys)
        except (ValueError, UnicodeError, TypeError):
            return jsonrpc_error(None, -32700, 'Invalid JSON')
        request_id, validation_error = validate_rpc(payload)
        if validation_error:
            code, message = validation_error
            return jsonrpc_error(request_id, code, message)
        tool = _failure_tool_name(payload)

        upstream_headers = {
            'Accept': 'application/json, text/event-stream',
            'Content-Type': 'application/json',
        }
        protocol_versions = request.headers.getall('MCP-Protocol-Version', [])
        if len(protocol_versions) > 1:
            return jsonrpc_error(request_id, -32600, 'Invalid MCP protocol version')
        protocol_version = protocol_versions[0] if protocol_versions else None
        if protocol_version is not None:
            if not _valid_protocol_version(protocol_version):
                return jsonrpc_error(request_id, -32600, 'Invalid MCP protocol version')
            upstream_headers['MCP-Protocol-Version'] = protocol_version
        session_ids = request.headers.getall('Mcp-Session-Id', [])
        if len(session_ids) > 1 or (session_ids and not _valid_mcp_header(session_ids[0])):
            return jsonrpc_error(request_id, -32600, 'Invalid MCP session id')
        if session_ids:
            upstream_headers['Mcp-Session-Id'] = session_ids[0]

        if self.inflight >= MAX_INFLIGHT:
            _log_failure('local_inflight_limit', 429, tool, self.inflight, started_at)
            return jsonrpc_error(request_id, -32000, 'Exa relay is busy', status=429)
        self.inflight += 1
        try:
            timeout = ClientTimeout(total=REQUEST_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS,
                                    sock_connect=CONNECT_TIMEOUT_SECONDS, sock_read=REQUEST_TIMEOUT_SECONDS)
            async with self.client.post(EXA_MCP_URL, headers=upstream_headers, data=bytes(body),
                                        allow_redirects=False, timeout=timeout) as upstream:
                response_headers = self._safe_response_headers(upstream)
                if upstream.status in (202, 204):
                    return web.Response(status=upstream.status, headers=response_headers)
                if 300 <= upstream.status < 400:
                    _log_failure('upstream_redirect', 502, tool, self.inflight, started_at)
                    return jsonrpc_error(request_id, -32000, 'Exa upstream redirect rejected', status=502)
                if upstream.status >= 400:
                    error_headers = {'Cache-Control': 'no-store'}
                    if 'Retry-After' in response_headers:
                        error_headers['Retry-After'] = response_headers['Retry-After']
                    retry_after = response_headers.get('Retry-After')
                    _log_failure('upstream_http_error', upstream.status, tool, self.inflight,
                                 started_at, int(retry_after) if retry_after is not None else None)
                    return jsonrpc_error(request_id, -32000, 'Exa upstream request failed',
                                         status=upstream.status, headers=error_headers)
                content_type = upstream.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                if content_type == 'text/event-stream':
                    if isinstance(payload, dict) and payload.get('method') == 'tools/list':
                        response_body = bytearray()
                        async for block in upstream.content.iter_chunked(16384):
                            response_body.extend(block)
                            if len(response_body) > MAX_RESPONSE_BYTES:
                                _log_failure('upstream_response_too_large', 502, tool,
                                             self.inflight, started_at)
                                return jsonrpc_error(request_id, -32000, 'Exa upstream response is too large', status=502)
                        try:
                            bounded = _rewrite_tool_catalog_sse(bytes(response_body))
                        except ValueError:
                            _log_failure('upstream_tool_catalog_invalid', 502, tool,
                                         self.inflight, started_at)
                            return jsonrpc_error(request_id, -32000, 'Exa tool catalog is incompatible', status=502)
                        if len(bounded) > MAX_RESPONSE_BYTES:
                            _log_failure('upstream_response_too_large', 502, tool,
                                         self.inflight, started_at)
                            return jsonrpc_error(request_id, -32000, 'Exa upstream response is too large', status=502)
                        response_headers['Content-Type'] = upstream.headers.get('Content-Type', 'text/event-stream')
                        return web.Response(body=bounded, status=upstream.status, headers=response_headers)
                    response_headers['Content-Type'] = upstream.headers.get('Content-Type', 'text/event-stream')
                    return await self._stream_sse(request, upstream, response_headers)
                if content_type != 'application/json':
                    _log_failure('upstream_content_type_invalid', 502, tool, self.inflight, started_at)
                    return jsonrpc_error(request_id, -32000, 'Invalid Exa upstream response', status=502)
                payload_out = bytearray()
                async for block in upstream.content.iter_chunked(65536):
                    payload_out.extend(block)
                    if len(payload_out) > MAX_RESPONSE_BYTES:
                        _log_failure('upstream_response_too_large', 502, tool,
                                     self.inflight, started_at)
                        return jsonrpc_error(request_id, -32000, 'Exa upstream response is too large', status=502)
                try:
                    parsed = json.loads(payload_out)
                    if not isinstance(parsed, (dict, list)):
                        raise ValueError
                except (ValueError, UnicodeError, TypeError):
                    _log_failure('upstream_json_invalid', 502, tool, self.inflight, started_at)
                    return jsonrpc_error(request_id, -32000, 'Invalid Exa upstream response', status=502)
                if isinstance(payload, dict) and payload.get('method') == 'tools/list':
                    try:
                        parsed = _bounded_tool_catalog(parsed)
                    except ValueError:
                        _log_failure('upstream_tool_catalog_invalid', 502, tool,
                                     self.inflight, started_at)
                        return jsonrpc_error(request_id, -32000, 'Exa tool catalog is incompatible', status=502)
                    payload_out = bytearray(json.dumps(parsed, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))
                    if len(payload_out) > MAX_RESPONSE_BYTES:
                        _log_failure('upstream_response_too_large', 502, tool,
                                     self.inflight, started_at)
                        return jsonrpc_error(request_id, -32000, 'Exa upstream response is too large', status=502)
                response_headers['Content-Type'] = upstream.headers.get('Content-Type', 'application/json')
                return web.Response(body=bytes(payload_out), status=upstream.status, headers=response_headers)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            _log_failure('upstream_timeout', 504, tool, self.inflight, started_at)
            return jsonrpc_error(request_id, -32000, 'Exa upstream timed out', status=504)
        except Exception:
            _log_failure('upstream_transport_error', 502, tool, self.inflight, started_at)
            return jsonrpc_error(request_id, -32000, 'Exa upstream unavailable', status=502)
        finally:
            self.inflight -= 1

    async def _stream_sse(self, request, upstream, headers):
        response = web.StreamResponse(status=200, headers=headers)
        await response.prepare(request)
        total = 0
        try:
            async for block in upstream.content.iter_chunked(16384):
                total += len(block)
                if total > MAX_RESPONSE_BYTES:
                    if request.transport:
                        request.transport.close()
                    return response
                await response.write(block)
            await response.write_eof()
        except asyncio.CancelledError:
            raise
        except Exception:
            if request.transport:
                request.transport.close()
        return response

    @staticmethod
    def _safe_response_headers(upstream):
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        session_id = upstream.headers.get('Mcp-Session-Id')
        if _valid_mcp_header(session_id):
            headers['Mcp-Session-Id'] = session_id
        protocol_version = upstream.headers.get('MCP-Protocol-Version')
        if protocol_version and _valid_protocol_version(protocol_version):
            headers['MCP-Protocol-Version'] = protocol_version
        retry_after = upstream.headers.get('Retry-After')
        if retry_after and re.fullmatch(r'\d{1,6}', retry_after):
            headers['Retry-After'] = retry_after
        return headers

    def app(self):
        app = web.Application(client_max_size=MAX_REQUEST_BYTES)
        app.cleanup_ctx.append(self.lifecycle)
        app.router.add_get('/health', self.health)
        app.router.add_get('/mcp/exa/capabilities', self.capabilities)
        app.router.add_route('*', '/mcp/exa', self.handle)
        return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8327)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    try:
        key_hash = load_key_hash(args.config)
    except ValueError as error:
        parser.error(str(error))
    proxy = ExaMcpProxy(key_hash)
    web.run_app(proxy.app(), host='127.0.0.1', port=args.port, access_log=None, print=None,
                handler_cancellation=True)


if __name__ == '__main__':
    main()
