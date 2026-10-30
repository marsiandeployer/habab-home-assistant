"""Real localhost HTTP/WebSocket integration tests; no HA or external services."""
import asyncio
import contextlib
import gzip
import json
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import WSMsgType, web

import web_tunnel as wt

HOME = "home-test.example.org"
OTHER = "other.example.org"
TOKEN = "a" * 48
TOKEN2 = "b" * 48


class TunnelTests(unittest.IsolatedAsyncioTestCase):
    async def start_app(self, app):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.runners.append(runner)
        return "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])

    async def asyncSetUp(self):
        self.runners = []
        self.release_stream = asyncio.Event()
        self.burst_release = asyncio.Event()
        self.burst_active = 0
        self.burst_peak = 0
        self.burst_slots_peak = 0
        self.extra_agents = []
        self.hit_paths = []
        self.upstream_headers = []
        self.ws_closed = asyncio.Event()
        async def fixture(request):
            self.hit_paths.append(request.raw_path)
            self.upstream_headers.append(dict(request.headers))
            if request.path == "/api/websocket":
                ws = web.WebSocketResponse(protocols=["ha"], autoclose=False)
                await ws.prepare(request)
                await ws.send_str('{"type":"auth_required"}')
                try:
                    while True:
                        msg = await ws.receive()
                        if msg.type == WSMsgType.TEXT:
                            if msg.data == "close":
                                await ws.close(code=1008, message=b"native reason")
                                break
                            await ws.send_str("echo:" + msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await ws.send_bytes(msg.data[::-1])
                        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                            await ws.close(code=msg.data if msg.type == WSMsgType.CLOSE else 1001)
                            break
                finally:
                    self.ws_closed.set()
                return ws
            if request.path.startswith("/burst/"):
                self.burst_active += 1
                self.burst_peak = max(self.burst_peak, self.burst_active)
                self.burst_slots_peak = max(self.burst_slots_peak, len(self.relay.slots))
                try:
                    await self.burst_release.wait()
                    return web.Response(text=request.path)
                finally:
                    self.burst_active -= 1
            if request.path == "/slow-static":
                response = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
                await response.prepare(request)
                await response.write(b"first")
                await self.release_stream.wait()
                with contextlib.suppress(ConnectionError):
                    await response.write(b"second")
                    await response.write_eof()
                return response
            if request.path == "/cookies":
                response = web.Response(text=request.headers.get("Cookie", "none"))
                response.headers.add("Set-Cookie", "one=1; HttpOnly; SameSite=Lax")
                response.headers.add("Set-Cookie", "two=2; Secure")
                return response
            if request.path == "/gzip":
                return web.Response(body=gzip.compress(b"static-file" * 500),
                                    headers={"Content-Encoding": "gzip", "ETag": '"asset-v1"'})
            if request.path == "/redirect":
                raise web.HTTPFound("http://192.0.2.1:8123/blocked")
            if request.path == "/auth/token":
                return web.Response(body=await request.read(), status=401,
                                    headers={"Content-Type": "application/json", "WWW-Authenticate": "Bearer"})
            if request.path == "/gzip-upload":
                return web.Response(body=await request.read())
            if request.headers.get("Authorization") != "Bearer native-ha-secret":
                return web.Response(text="native login", status=401)
            return web.json_response({"raw": request.raw_path, "host": request.host})
        upstream = web.Application(handler_args={"auto_decompress": False})
        upstream.router.add_route("*", "/{path:.*}", fixture)
        self.target = await self.start_app(upstream)
        self.relay = wt.Relay({HOME: TOKEN, OTHER: TOKEN2})
        self.base = await self.start_app(self.relay.app())
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False)
        self.agent = asyncio.create_task(wt.run_agent(
            self.session, self.base.replace("http:", "ws:") + wt.PREFIX + "/control",
            HOME, TOKEN, target=self.target, allow_insecure_loopback=True))
        await self.online()

    async def online(self, home=HOME):
        for _ in range(200):
            if home in self.relay.controls and self.relay.controls[home].prepared:
                return
            await asyncio.sleep(.01)
        self.fail("agent did not connect")

    async def asyncTearDown(self):
        self.release_stream.set()
        self.burst_release.set()
        for task in self.extra_agents:
            task.cancel()
        await asyncio.gather(*self.extra_agents, return_exceptions=True)
        self.agent.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.agent
        await self.session.close()
        for runner in reversed(self.runners):
            await runner.cleanup()

    async def get(self, path="/", *, headers=None, **kwargs):
        return await self.session.get(self.base + path, headers={"Host": HOME, **(headers or {})}, **kwargs)

    async def test_native_login_and_raw_rest(self):
        async with await self.get() as response:
            self.assertEqual(response.status, 401)
            self.assertEqual(await response.text(), "native login")
        async with await self.get("/api/test%2Fencoded?q=%2B&other=a%20b", headers={
            "Authorization": "Bearer native-ha-secret", "Origin": "https://" + HOME,
            "Forwarded": "for=evil", "X-Forwarded-For": "evil", "X-Forwarded-Proto": "http",
        }) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["raw"], "/api/test%2Fencoded?q=%2B&other=a%20b")
        seen = self.upstream_headers[-1]
        self.assertEqual(seen["Host"], HOME)
        self.assertEqual(seen["Origin"], "https://" + HOME)
        self.assertFalse(any(key.lower().startswith("x-forwarded-") or key.lower() == "forwarded" for key in seen))

    async def test_post_auth_and_compressed_upload(self):
        payload = b'{"grant_type":"authorization_code","code":"opaque"}'
        async with self.session.post(self.base + "/auth/token", headers={"Host": HOME}, data=payload) as response:
            self.assertEqual(response.status, 401)
            self.assertEqual(await response.read(), payload)
            self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        compressed = gzip.compress(b"request-body" * 20)
        async with self.session.post(self.base + "/gzip-upload", headers={"Host": HOME, "Content-Encoding": "gzip"}, data=compressed) as response:
            self.assertEqual(await response.read(), compressed)

    async def test_repeated_cookies_and_no_cross_client_cookie_jar(self):
        async with await self.get("/cookies", headers={"Cookie": "browser=owned"}) as response:
            self.assertEqual(len(response.headers.getall("Set-Cookie")), 2)
            self.assertEqual(await response.text(), "browser=owned")
        async with await self.get("/cookies") as response:
            self.assertEqual(await response.text(), "none")

    async def test_static_compression_and_streaming_backpressure(self):
        async with await self.get("/gzip") as response:
            self.assertEqual(response.headers["Content-Encoding"], "gzip")
            self.assertEqual(response.headers["ETag"], '"asset-v1"')
            self.assertEqual(gzip.decompress(await response.read()), b"static-file" * 500)
        async with await self.get("/slow-static") as response:
            self.assertEqual(await asyncio.wait_for(response.content.readexactly(5), 2), b"first")
            self.release_stream.set()
            self.assertEqual(await response.read(), b"second")

    async def test_websocket_text_binary_subprotocol_and_remote_close(self):
        async with self.session.ws_connect(self.base + "/api/websocket", headers={"Host": HOME}, protocols=["ha"], autoclose=False) as ws:
            self.assertEqual(ws.protocol, "ha")
            self.assertEqual((await ws.receive()).data, '{"type":"auth_required"}')
            await ws.send_str('{"type":"auth","access_token":"native"}')
            self.assertEqual((await ws.receive()).data, 'echo:{"type":"auth","access_token":"native"}')
            await ws.send_bytes(b"\x00\x01\x02")
            self.assertEqual((await ws.receive()).data, b"\x02\x01\x00")
            await ws.send_str("close")
            closed = await asyncio.wait_for(ws.receive(), 2)
            self.assertEqual(closed.type, WSMsgType.CLOSE)
            self.assertEqual(closed.data, 1008)
            self.assertEqual(closed.extra, "native reason")

    async def test_browser_close_cancels_upstream(self):
        ws = await self.session.ws_connect(self.base + "/api/websocket", headers={"Host": HOME})
        await ws.receive()
        await ws.close(code=1000)
        await asyncio.wait_for(self.ws_closed.wait(), 2)
        for _ in range(100):
            if not self.relay.slots:
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.relay.slots, {})

    async def test_offline_unknown_host_and_reconnect(self):
        async with await self.get(headers={"Host": "unknown.example.org"}) as response:
            self.assertEqual(response.status, 421)
        async with await self.get(headers={"Host": OTHER}) as response:
            self.assertEqual(response.status, 503)
        await self.relay.controls[HOME].close(code=1001)
        await asyncio.sleep(.05)
        async with await self.get() as response:
            self.assertEqual(response.status, 503)
        await self.online()
        async with await self.get() as response:
            self.assertEqual(response.status, 401)

    async def test_unauthenticated_tunnel_and_cross_home_stream(self):
        control_url = self.base + wt.PREFIX + "/control"
        for headers in ({}, {"X-Habab-Home": HOME, "Authorization": "Bearer wrong"},
                        {"X-Habab-Home": OTHER, "Authorization": "Bearer " + TOKEN}):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
                await self.session.ws_connect(control_url, headers=headers)
            self.assertEqual(caught.exception.status, 401)
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.session.ws_connect(control_url, headers={"X-Habab-Home": HOME, "Authorization": "Bearer " + TOKEN})
        self.assertEqual(caught.exception.status, 409)
        async with self.session.get(self.base + wt.PREFIX + "/data/" + "a" * 48,
                                    headers={"X-Habab-Home": OTHER, "Authorization": "Bearer " + TOKEN2}) as response:
            self.assertEqual(response.status, 404)

    async def test_redirect_not_followed_and_target_locked(self):
        async with await self.get("/redirect", allow_redirects=False) as response:
            self.assertEqual(response.status, 302)
            self.assertEqual(response.headers["Location"], "http://192.0.2.1:8123/blocked")
        for path in ("//192.0.2.1/", "http://192.0.2.1/", "/\\evil", "/foo#bar", "/\x00"):
            with self.assertRaises(ValueError):
                wt.request_url(self.target, path)
        for target in ("http://192.0.2.1:8123", "http://localhost:8123", "http://127.0.0.1:8123/path", "https://127.0.0.1"):
            with self.assertRaises(ValueError):
                wt.local_target(target)
        with self.assertRaises(ValueError):
            wt.validate_relay("ws://example.org" + wt.PREFIX + "/control")

    async def test_concurrency_and_request_size_limits(self):
        old_streams = wt.MAX_STREAMS
        old_body = wt.MAX_BODY
        wt.MAX_STREAMS = 1
        try:
            async with await self.get("/slow-static") as first:
                self.assertEqual(await first.content.readexactly(5), b"first")
                with patch.object(wt, "QUEUE_TIMEOUT", .05):
                    async with await self.get() as second:
                        self.assertEqual(second.status, 503)
                        self.assertEqual(await second.text(), "Home tunnel is busy\n")
                self.release_stream.set()
                await first.read()
            wt.MAX_BODY = 8
            async with self.session.post(self.base + "/auth/token", headers={"Host": HOME}, data=b"longer-than-eight") as response:
                self.assertEqual(response.status, 413)
        finally:
            wt.MAX_STREAMS = old_streams
            wt.MAX_BODY = old_body

    async def wait_until(self, predicate):
        for _ in range(300):
            if predicate():
                return
            await asyncio.sleep(.01)
        self.fail("condition did not become true")

    async def test_cold_browser_burst_queues_with_live_websocket(self):
        ws = await self.session.ws_connect(self.base + "/api/websocket", headers={"Host": HOME})
        await ws.receive()
        async def fetch(index):
            async with await self.get("/burst/" + str(index)) as response:
                return response.status, await response.text()
        requests = [asyncio.create_task(fetch(index)) for index in range(40)]
        try:
            await self.wait_until(lambda: self.relay.waiters.get(HOME) == len(requests) - (wt.MAX_STREAMS - 1))
            self.assertEqual(len(self.relay.slots), wt.MAX_STREAMS)
            self.assertEqual(self.burst_active, wt.MAX_STREAMS - 1)
            self.assertFalse(any(task.done() for task in requests))
            self.burst_release.set()
            results = await asyncio.wait_for(asyncio.gather(*requests), 10)
            self.assertEqual(results, [(200, "/burst/" + str(index)) for index in range(40)])
            self.assertLessEqual(self.burst_peak, wt.MAX_STREAMS - 1)
            self.assertLessEqual(self.burst_slots_peak, wt.MAX_STREAMS)
            await self.wait_until(lambda: len(self.relay.slots) == 1)
            self.assertEqual(self.relay.waiters, {})
        finally:
            self.burst_release.set()
            await wt.cancel_tasks(requests)
            await ws.close()

    async def test_wait_queue_is_bounded_and_cancelled_browser_is_removed(self):
        with patch.object(wt, "MAX_STREAMS", 1), patch.object(wt, "MAX_WAITERS", 1):
            async with await self.get("/slow-static") as first:
                await first.content.readexactly(5)
                queued = asyncio.create_task(self.get())
                try:
                    await self.wait_until(lambda: self.relay.waiters.get(HOME) == 1)
                    async with await self.get() as overflow:
                        self.assertEqual(overflow.status, 503)
                        self.assertEqual(await overflow.text(), "Home tunnel is busy\n")
                    queued.cancel()
                    await asyncio.gather(queued, return_exceptions=True)
                    await self.wait_until(lambda: not self.relay.waiters)
                    self.assertEqual(len(self.relay.slots), 1)
                finally:
                    await wt.cancel_tasks([queued])
                    self.release_stream.set()
                    await first.read()
            await self.wait_until(lambda: not self.relay.slots)

    async def test_disconnect_wakes_waiting_requests_as_offline(self):
        with patch.object(wt, "MAX_STREAMS", 1):
            async with await self.get("/slow-static") as first:
                await first.content.readexactly(5)
                queued = asyncio.create_task(self.get())
                try:
                    await self.wait_until(lambda: self.relay.waiters.get(HOME) == 1)
                    await self.relay.controls[HOME].close(code=1001)
                    async with await asyncio.wait_for(queued, 2) as response:
                        self.assertEqual(response.status, 503)
                        self.assertEqual(await response.text(), "Home is offline\n")
                    self.assertEqual(self.relay.waiters, {})
                finally:
                    self.release_stream.set()
                    await wt.cancel_tasks([queued])
            await self.wait_until(lambda: not self.relay.slots)

    async def test_busy_home_does_not_hold_global_capacity_from_other_home(self):
        other = asyncio.create_task(wt.run_agent(
            self.session, self.base.replace("http:", "ws:") + wt.PREFIX + "/control",
            OTHER, TOKEN2, target=self.target, allow_insecure_loopback=True))
        self.extra_agents.append(other)
        await self.online(OTHER)
        with patch.object(wt, "MAX_STREAMS", 1), patch.object(wt, "MAX_TOTAL", 2):
            async with await self.get("/slow-static") as first:
                await first.content.readexactly(5)
                queued = asyncio.create_task(self.get())
                try:
                    await self.wait_until(lambda: self.relay.waiters.get(HOME) == 1)
                    async with await asyncio.wait_for(self.get(headers={
                        "Host": OTHER, "Authorization": "Bearer native-ha-secret"}), 2) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual((await response.json())["host"], OTHER)
                    self.assertFalse(queued.done())
                    self.release_stream.set()
                    await first.read()
                    async with await asyncio.wait_for(queued, 2) as response:
                        self.assertEqual(response.status, 401)
                finally:
                    self.release_stream.set()
                    await wt.cancel_tasks([queued])

    async def test_global_active_and_pending_limits_apply_across_homes(self):
        other = asyncio.create_task(wt.run_agent(
            self.session, self.base.replace("http:", "ws:") + wt.PREFIX + "/control",
            OTHER, TOKEN2, target=self.target, allow_insecure_loopback=True))
        self.extra_agents.append(other)
        await self.online(OTHER)
        with (patch.object(wt, "MAX_STREAMS", 2), patch.object(wt, "MAX_TOTAL", 1),
              patch.object(wt, "MAX_WAITERS_TOTAL", 1)):
            async with await self.get("/slow-static") as first:
                await first.content.readexactly(5)
                queued = asyncio.create_task(self.get(headers={"Host": OTHER}))
                try:
                    await self.wait_until(lambda: self.relay.waiters.get(OTHER) == 1)
                    self.assertEqual(len(self.relay.slots), 1)
                    async with await self.get() as overflow:
                        self.assertEqual(overflow.status, 503)
                        self.assertEqual(await overflow.text(), "Home tunnel is busy\n")
                    self.release_stream.set()
                    await first.read()
                    async with await asyncio.wait_for(queued, 2) as response:
                        self.assertEqual(response.status, 401)
                    self.assertEqual(self.relay.waiters, {})
                finally:
                    self.release_stream.set()
                    await wt.cancel_tasks([queued])

    async def test_response_failure_log_excludes_secret_exception_text(self):
        secret = "Bearer private-ha-token Cookie=private-browser-session"
        original = wt.packet
        async def fail_response_metadata(ws, *args, **kwargs):
            if isinstance(ws, web.WebSocketResponse):
                raise ValueError(secret)
            return await original(ws, *args, **kwargs)
        with patch.object(wt, "packet", side_effect=fail_response_metadata):
            with self.assertLogs(wt.LOG, level="WARNING") as captured:
                async with await self.get() as response:
                    self.assertEqual(response.status, 502)
                    self.assertEqual(await response.text(), "Home tunnel unavailable\n")
        self.assertEqual(captured.output, [
            "WARNING:habab.web_tunnel:Home tunnel failed phase=response_headers error=ValueError"])
        self.assertNotIn(secret, " ".join(captured.output))

    async def test_agent_connect_timeout_has_distinct_sanitized_phase(self):
        with (patch.object(wt, "OPEN_TIMEOUT", .02),
              patch.object(self.relay.controls[HOME], "send_json", new=AsyncMock())):
            with self.assertLogs(wt.LOG, level="WARNING") as captured:
                async with await self.get() as response:
                    self.assertEqual(response.status, 502)
                    self.assertEqual(await response.text(), "Home tunnel unavailable\n")
        self.assertEqual(captured.output, [
            "WARNING:habab.web_tunnel:Home tunnel failed phase=agent_connect error=TimeoutError"])
        await self.wait_until(lambda: not self.relay.slots)


if __name__ == "__main__":
    unittest.main()
