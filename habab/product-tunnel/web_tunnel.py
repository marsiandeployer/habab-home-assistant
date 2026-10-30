#!/usr/bin/env python3
"""Outbound-only HA web relay. See README-web.md for trust/deployment boundaries."""
from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web
from multidict import CIMultiDict
from yarl import URL

PREFIX = "/_habab_tunnel"
CHUNK = 64 * 1024
MAX_BODY = 32 * 1024 * 1024
MAX_WS = 8 * 1024 * 1024
MAX_STREAMS = 8  # Pilot relay budget leaves cleanup headroom for native capacity 16.
MAX_AGENT_STREAMS = 16
MAX_TOTAL = 128
MAX_WAITERS = 256
MAX_WAITERS_TOTAL = 1024
QUEUE_TIMEOUT = 60
OPEN_TIMEOUT = 15
HTTP_TIMEOUT = 180
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade"}
LOG = logging.getLogger("habab.web_tunnel")


def hostname(value: str) -> str:
    """Exact DNS host matching; no wildcard/trailing-dot/IPv6 aliases."""
    match = re.fullmatch(r"([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::[0-9]{1,5})?", value.lower())
    if not match:
        raise ValueError("invalid home hostname")
    return match[1]


def filtered_headers(items, *, request=False, websocket=False):
    items = list(items)
    banned = HOP.copy()
    for key, value in items:
        if key.lower() == "connection":
            banned.update(part.strip().lower() for part in value.split(","))
    if websocket:
        banned.update({"sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
                       "sec-websocket-accept", "sec-websocket-protocol", "content-length"})
    if request:
        banned.update({"forwarded", "x-real-ip", "x-habab-home"})
    return [(key, value) for key, value in items
            if key.lower() not in banned
            and not (request and key.lower().startswith("x-forwarded-"))]


def local_target(value: str) -> str:
    url = URL(value)
    if (url.scheme != "http" or url.user is not None or url.query_string
            or url.fragment or url.path not in ("", "/") or not url.host):
        raise ValueError("target must be a loopback HTTP origin")
    try:
        valid = ipaddress.ip_address(url.host).is_loopback
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("target must use a literal loopback IP address")
    return str(url.origin())


def request_url(target: str, path: str) -> URL:
    if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
            or "\\" in path or "#" in path or any(ord(c) < 32 for c in path)):
        raise ValueError("invalid origin-form path")
    # Never URL-join a remote-provided path: it cannot change the local origin.
    return URL(target + path, encoded=True)


async def packet(ws, timeout=HTTP_TIMEOUT):
    message = await asyncio.wait_for(ws.receive(), timeout)
    if message.type != WSMsgType.TEXT:
        raise ConnectionError("expected protocol metadata")
    result = json.loads(message.data)
    if not isinstance(result, dict):
        raise ValueError("invalid protocol metadata")
    return result


async def cancel_tasks(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@dataclass
class Slot:
    home: str
    control: web.WebSocketResponse
    future: asyncio.Future
    done: asyncio.Event = field(default_factory=asyncio.Event)
    socket: web.WebSocketResponse | None = None


class Relay:
    def __init__(self, homes: dict[str, str]):
        if not homes:
            raise ValueError("at least one home is required")
        self.homes = {}
        for home, token in homes.items():
            if home != hostname(home) or not isinstance(token, str) or len(token) < 32:
                raise ValueError("canonical hostname and token of at least 32 characters required")
            if not token.isascii() or any(c.isspace() for c in token):
                raise ValueError("token must be opaque printable ASCII")
            self.homes[home] = token
        if len(set(self.homes.values())) != len(self.homes):
            raise ValueError("each home must have its own token")
        self.controls: dict[str, web.WebSocketResponse] = {}
        self.slots: dict[str, Slot] = {}
        self.capacity = asyncio.Condition()
        self.waiters: dict[str, int] = {}

    def authenticate(self, request):
        home = request.headers.get("X-Habab-Home", "")
        supplied = request.headers.get("Authorization", "")
        expected = self.homes.get(home)
        if expected is None or not hmac.compare_digest(supplied.encode(), ("Bearer " + expected).encode()):
            raise web.HTTPUnauthorized(text="Tunnel authentication required\n")
        return home

    async def control(self, request):
        home = self.authenticate(request)
        if home in self.controls:
            raise web.HTTPConflict(text="Home already connected\n")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=CHUNK)
        # Claim before prepare (which yields), so concurrent enrollment cannot win twice.
        self.controls[home] = ws
        try:
            await ws.prepare(request)
            await ws.send_json({"type": "ready"})
            async for _ in ws:
                # Control is server-to-agent only; receiving payload is a protocol error.
                await ws.close(code=1008, message=b"unexpected control message")
        finally:
            if self.controls.get(home) is ws:
                del self.controls[home]
            async with self.capacity:
                self.capacity.notify_all()
            for slot in list(self.slots.values()):
                if slot.control is ws:
                    if not slot.future.done():
                        slot.future.set_result(None)
                    if slot.socket:
                        await slot.socket.close(code=1001)
            await ws.close()
        return ws

    async def data(self, request):
        home = self.authenticate(request)
        slot = self.slots.get(request.match_info["stream"])
        if (slot is None or slot.home != home or slot.future.done()
                or slot.socket is not None or self.controls.get(home) is not slot.control):
            raise web.HTTPNotFound()
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_WS + CHUNK, autoclose=False)
        slot.socket = ws
        # Claim slot before prepare: a replay cannot attach a second data connection.
        try:
            await ws.prepare(request)
            slot.future.set_result(ws)
            # Public handler now owns all reads/writes on this data connection.
            await slot.done.wait()
        finally:
            await ws.close()
        return ws

    async def reserve_slot(self, request, home, control):
        """Wait for both limits together, without allocating an agent/data stream."""
        deadline = asyncio.get_running_loop().time() + QUEUE_TIMEOUT
        waiting = False
        async with self.capacity:
            try:
                while True:
                    if self.controls.get(home) is not control or control.closed:
                        raise web.HTTPServiceUnavailable(text="Home is offline\n")
                    if request.transport is None or request.transport.is_closing():
                        raise asyncio.CancelledError
                    if (len(self.slots) < MAX_TOTAL
                            and sum(slot.home == home for slot in self.slots.values()) < MAX_STREAMS):
                        stream = secrets.token_hex(24)
                        slot = Slot(home, control, asyncio.get_running_loop().create_future())
                        self.slots[stream] = slot
                        return stream, slot
                    if not waiting:
                        if (self.waiters.get(home, 0) >= MAX_WAITERS
                                or sum(self.waiters.values()) >= MAX_WAITERS_TOTAL):
                            raise web.HTTPServiceUnavailable(text="Home tunnel is busy\n")
                        self.waiters[home] = self.waiters.get(home, 0) + 1
                        waiting = True
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise web.HTTPServiceUnavailable(text="Home tunnel is busy\n")
                    try:
                        # aiohttp need not cancel a handler when its browser disconnects.
                        # Recheck its transport while keeping the queue wait bounded.
                        await asyncio.wait_for(self.capacity.wait(), min(remaining, 1))
                    except asyncio.TimeoutError:
                        pass
            finally:
                if waiting:
                    self.waiters[home] -= 1
                    if not self.waiters[home]:
                        del self.waiters[home]

    async def public(self, request):
        try:
            home = hostname(request.headers.get("Host", ""))
        except ValueError:
            raise web.HTTPMisdirectedRequest()
        if home not in self.homes:
            raise web.HTTPMisdirectedRequest()
        control = self.controls.get(home)
        if control is None or not control.prepared or control.closed:
            raise web.HTTPServiceUnavailable(text="Home is offline\n")
        if request.content_length is not None and request.content_length > MAX_BODY:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY, actual_size=request.content_length)
        stream, slot = await self.reserve_slot(request, home, control)
        upload = None
        response = None
        phase = "agent_connect"
        try:
            await control.send_json({"type": "open", "stream": stream})
            ws = await asyncio.wait_for(asyncio.shield(slot.future), OPEN_TIMEOUT)
            if ws is None:
                raise ConnectionError("home disconnected")
            phase = "request_metadata"
            is_ws = request.headers.get("Upgrade", "").lower() == "websocket"
            headers = filtered_headers(request.headers.items(), request=True, websocket=is_ws)
            await ws.send_json({"type": "request", "method": request.method, "path": request.raw_path,
                                "headers": headers, "websocket": is_ws,
                                "protocols": request.headers.get("Sec-WebSocket-Protocol", "")})
            if not is_ws:
                upload = asyncio.create_task(self.upload(request, ws))
            phase = "response_headers"
            meta = await packet(ws)
            if meta.get("type") != "response":
                raise ValueError("invalid response metadata")
            status = int(meta["status"])
            if is_ws and status == 101:
                protocols = [meta["protocol"]] if meta.get("protocol") else []
                response = web.WebSocketResponse(protocols=protocols, max_msg_size=MAX_WS,
                                                 autoclose=False, heartbeat=30)
                await response.prepare(request)
                phase = "websocket"
                await bridge(response, ws)
                return response
            response = web.StreamResponse(status=status, headers=CIMultiDict(
                filtered_headers(meta.get("headers", []))))
            await response.prepare(request)
            phase = "response_body"
            total = 0
            while True:
                message = await asyncio.wait_for(ws.receive(), HTTP_TIMEOUT)
                if message.type == WSMsgType.BINARY:
                    total += len(message.data)
                    if total > MAX_BODY:
                        raise ValueError("response limit exceeded")
                    await response.write(message.data)
                elif message.type == WSMsgType.TEXT and json.loads(message.data).get("type") == "end":
                    await response.write_eof()
                    return response
                else:
                    raise ConnectionError("incomplete home response")
        except (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError, ValueError, KeyError) as error:
            # Exception text, URLs, headers and request bodies may contain credentials.
            LOG.warning("Home tunnel failed phase=%s error=%s", phase, type(error).__name__)
            if response is not None and response.prepared:
                if request.transport:
                    request.transport.close()
                return response
            raise web.HTTPBadGateway(text="Home tunnel unavailable\n")
        finally:
            if upload:
                await cancel_tasks([upload])
            if not slot.future.done():
                slot.future.cancel()
            slot.done.set()
            try:
                if slot.socket:
                    await slot.socket.close()
            finally:
                async with self.capacity:
                    self.slots.pop(stream, None)
                    self.capacity.notify_all()

    @staticmethod
    async def upload(request, ws):
        try:
            total = 0
            async for chunk in request.content.iter_chunked(CHUNK):
                total += len(chunk)
                if total > MAX_BODY:
                    await ws.close(code=1009, message=b"request too large")
                    return
                await ws.send_bytes(chunk)
            await ws.send_json({"type": "end"})
        except (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError):
            await ws.close(code=1001)

    async def shutdown(self, app):
        await asyncio.gather(*(ws.close(code=1001) for ws in list(self.controls.values())),
                             return_exceptions=True)
        for slot in list(self.slots.values()):
            slot.done.set()
            if not slot.future.done():
                slot.future.set_result(None)
            if slot.socket:
                await slot.socket.close(code=1001)

    def app(self):
        app = web.Application(client_max_size=MAX_BODY, handler_args={"auto_decompress": False})
        app.router.add_get(PREFIX + "/control", self.control)
        app.router.add_get(PREFIX + "/data/{stream}", self.data)
        app.router.add_route("*", "/{path:.*}", self.public)
        app.on_shutdown.append(self.shutdown)
        return app


async def native_to_tunnel(native, tunnel):
    while True:
        message = await native.receive()
        if message.type == WSMsgType.TEXT:
            await tunnel.send_json({"type": "text", "data": message.data})
        elif message.type == WSMsgType.BINARY:
            await tunnel.send_bytes(message.data)
        elif message.type == WSMsgType.CLOSE:
            await tunnel.send_json({"type": "close", "code": message.data, "reason": message.extra})
            return
        elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSED):
            return


async def tunnel_to_native(tunnel, native):
    while True:
        message = await tunnel.receive()
        if message.type == WSMsgType.BINARY:
            await native.send_bytes(message.data)
        elif message.type == WSMsgType.TEXT:
            data = json.loads(message.data)
            if data.get("type") == "text" and isinstance(data.get("data"), str):
                await native.send_str(data["data"])
            elif data.get("type") == "close":
                code = int(data.get("code", 1000))
                # 1005/1006/1015 are receive-only and cannot be put on the wire.
                if code in (1005, 1006, 1015):
                    code = 1001
                await native.close(code=code, message=str(data.get("reason", "")).encode()[:123])
                return
            else:
                raise ValueError("invalid websocket frame")
        elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
            await native.close(code=1001)
            return


async def bridge(native, tunnel):
    tasks = [asyncio.create_task(native_to_tunnel(native, tunnel)),
             asyncio.create_task(tunnel_to_native(tunnel, native))]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        await cancel_tasks(tasks)
        await native.close()


async def body_from_tunnel(ws):
    total = 0
    while True:
        message = await asyncio.wait_for(ws.receive(), HTTP_TIMEOUT)
        if message.type == WSMsgType.BINARY:
            total += len(message.data)
            if total > MAX_BODY:
                raise ValueError("request body limit exceeded")
            yield message.data
        elif message.type == WSMsgType.TEXT and json.loads(message.data).get("type") == "end":
            return
        else:
            raise ConnectionError("incomplete request body")


async def serve_stream(session, local, url, auth, target):
    tunnel = await asyncio.wait_for(session.ws_connect(
        url, headers=auth, heartbeat=20, max_msg_size=MAX_WS + CHUNK, autoclose=False), OPEN_TIMEOUT)
    async with tunnel:
        try:
            meta = await packet(tunnel, OPEN_TIMEOUT)
            if meta.get("type") != "request":
                raise ValueError("invalid request")
            url = request_url(target, meta["path"])
            headers = filtered_headers(meta.get("headers", []), request=True,
                                       websocket=bool(meta.get("websocket")))
            if meta.get("websocket"):
                protocols = [p.strip() for p in meta.get("protocols", "").split(",") if p.strip()]
                upstream = await asyncio.wait_for(local.ws_connect(
                    url, headers=headers, protocols=protocols,
                    max_msg_size=MAX_WS, autoclose=False), OPEN_TIMEOUT)
                async with upstream:
                    await tunnel.send_json({"type": "response", "status": 101,
                                            "protocol": upstream.protocol})
                    await bridge(upstream, tunnel)
            else:
                async with local.request(meta["method"], url, headers=headers,
                                         data=body_from_tunnel(tunnel), allow_redirects=False) as upstream:
                    if upstream.content_length is not None and upstream.content_length > MAX_BODY:
                        raise ValueError("upstream response too large")
                    await tunnel.send_json({"type": "response", "status": upstream.status,
                                            "headers": filtered_headers(upstream.headers.items())})
                    total = 0
                    async for chunk in upstream.content.iter_chunked(CHUNK):
                        total += len(chunk)
                        if total > MAX_BODY:
                            raise ValueError("upstream response too large")
                        await tunnel.send_bytes(chunk)
                    await tunnel.send_json({"type": "end"})
        except aiohttp.WSServerHandshakeError as error:
            if not tunnel.closed:
                await tunnel.send_json({"type": "response", "status": error.status,
                                        "headers": filtered_headers((error.headers or {}).items(), websocket=True)})
                await tunnel.send_json({"type": "end"})
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, ValueError, KeyError):
            if not tunnel.closed:
                # Never log exceptions: aiohttp exception strings may contain credentials/URLs.
                await tunnel.close(code=1011, message=b"local request failed")


async def reject_redirect(session, context, params):
    # aiohttp ws_connect follows redirects internally; abort before the next request.
    raise ValueError("upstream redirects are not followed")


def validate_relay(value, *, allow_insecure_loopback=False):
    url = URL(value)
    if url.user is not None or url.query_string or url.fragment or url.path != PREFIX + "/control":
        raise ValueError("relay must end with " + PREFIX + "/control")
    if url.scheme != "wss":
        try:
            allowed = allow_insecure_loopback and url.scheme == "ws" and ipaddress.ip_address(url.host).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            raise ValueError("relay requires verified WSS")
    return url


async def run_agent(session: aiohttp.ClientSession, relay_url: str, home: str, token: str, *,
                    target="http://127.0.0.1:8123", allow_insecure_loopback=False):
    """Run until cancelled; session is caller-owned and must not contain HA credentials.

    target/allow_insecure_loopback are operator/test-only arguments, never network input.
    Reconnects indefinitely with capped backoff; cancellation closes all stream tasks.
    """
    relay = validate_relay(relay_url, allow_insecure_loopback=allow_insecure_loopback)
    target = local_target(target)
    if home != hostname(home) or not token or "\n" in token or "\r" in token:
        raise ValueError("invalid home identity")
    auth = {"Authorization": "Bearer " + token, "X-Habab-Home": home}
    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(reject_redirect)
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT, connect=OPEN_TIMEOUT, sock_read=HTTP_TIMEOUT)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
                                     trust_env=False, timeout=timeout, trace_configs=[trace],
                                     connector=aiohttp.TCPConnector(limit=MAX_AGENT_STREAMS)) as local:
        delay = 1
        while True:
            tasks = set()
            try:
                control = await asyncio.wait_for(session.ws_connect(
                    relay, headers=auth, heartbeat=20, max_msg_size=CHUNK), OPEN_TIMEOUT)
                async with control:
                    ready = await packet(control, OPEN_TIMEOUT)
                    if ready.get("type") != "ready":
                        raise ValueError("relay handshake failed")
                    delay = 1
                    async for message in control:
                        if message.type != WSMsgType.TEXT:
                            continue
                        data = json.loads(message.data)
                        stream = data.get("stream", "")
                        if (data.get("type") != "open" or not re.fullmatch(r"[a-f0-9]{48}", stream)
                                or len(tasks) >= MAX_AGENT_STREAMS):
                            raise ValueError("invalid stream allocation")
                        data_url = relay.with_path(PREFIX + "/data/" + stream)
                        task = asyncio.create_task(serve_stream(session, local, data_url, auth, target))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, ValueError, KeyError):
                LOG.warning("Home tunnel disconnected; reconnecting")
            finally:
                await cancel_tasks(tasks)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    relay = commands.add_parser("relay")
    relay.add_argument("--config", required=True)
    relay.add_argument("--host", default="127.0.0.1")
    relay.add_argument("--port", type=int, default=18330)
    agent = commands.add_parser("agent")
    agent.add_argument("--relay", required=True)
    agent.add_argument("--home", required=True)
    agent.add_argument("--token-file", required=True)
    agent.add_argument("--target", default="http://127.0.0.1:8123")
    args = parser.parse_args()
    if args.command == "relay":
        if not ipaddress.ip_address(args.host).is_loopback:
            parser.error("relay may listen only on loopback behind HTTPS nginx")
        config = json.loads(Path(args.config).read_text())
        homes = {home: entry["token"] for home, entry in config["homes"].items()}
        web.run_app(Relay(homes).app(), host=args.host, port=args.port, access_log=None)
    else:
        async def start():
            async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), trust_env=False) as session:
                await run_agent(session, args.relay, args.home, Path(args.token_file).read_text().strip(),
                                target=args.target)
        asyncio.run(start())


if __name__ == "__main__":
    main()
