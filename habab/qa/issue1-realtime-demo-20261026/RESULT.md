# Issue #1 — public demo Realtime source candidate (2026-10-26)

**Issue:** [#1 Голосовой помощник: разговор на русском и команды демо через OpenAI Realtime](https://github.com/marsiandeployer/habab-home-assistant/issues/1), OPEN.

## Candidate source path

- `habab/habab_demo/routes.py` adds same-origin `POST /api/ha-demo/realtime/bootstrap`. It accepts no client context, binds a grant to the existing anonymous demo session hash, and passes the server's `OPENAI_API_KEY` only through the local relay control socket. It returns only an opaque 60-second one-use token and the fixed Habab WebSocket URL.
- `habab/android/relay/openai_gateway.py` accepts that purpose-specific local command, keeps the OpenAI key in process memory, enforces the configured exact browser Origin and one-use token, and opens a fixed server-to-server Realtime WebSocket. It supplies the server-owned Russian audio session/tools, refuses browser `session.update`, caps active voice connections at two, frames at 256 KiB, each direction at 16 MiB, and each connection at five minutes. It forwards only to OpenAI and back through Habab.
- `demo/src/realtime-voice.ts` is a demo-only panel. Microphone permission and audio capture begin only after the user clicks **Начать разговор**. PCM audio goes to the Habab voice WebSocket. Its three tools can write only allowlisted virtual lights, the demo climate entity, or the two demo scenes through the existing same-origin `/api/ha-demo/state` API. The existing backend validates and persists those writes within the current anonymous session, then the existing mock-entity seam updates the visible demo state.
- `demo/src/entrypoint.ts` loads the panel in the standalone demo.

Server limits are two shared active voice connections, a ten-second first-frame auth deadline, one-use 60-second grants, 256 KiB frames, 16 MiB cumulative traffic in each direction, and a five-minute connection timeout. The relay also enforces a configurable cumulative launch budget of five sessions per process and two launches per anonymous HMAC session by default, charging before opening the paid upstream socket. Counters are RAM-only and reset on relay restart; this is a small process-lifetime budget, not a durable daily quota. The virtual-state API retains its current 120 writes/minute/session cap.

The web app must run under the same Unix UID as the relay so its existing protected `control.sock` can bootstrap grants. The route currently defaults to `/root/.local/share/habab-voice-relay/control.sock`; deployments with another relay config path must set `HA_DEMO_REALTIME_CONTROL_SOCKET`. The `OPENAI_API_KEY` already present in the demo app environment must be eligible for Realtime access. No key was read or printed here.

## Verification

- Relay suite: `PYTHONDONTWRITEBYTECODE=1 /root/.venvs/habab-voice-relay/bin/python -m unittest discover -s habab/android/relay -p test_gateway.py -q` — **51 passed**. All upstream requests were intercepted by loopback fixtures; coverage includes sequential global-budget exhaustion and per-session launch-cap enforcement.
- Demo route suite: `PYTHONPATH=habab PYTHONDONTWRITEBYTECODE=1 python3 -m unittest habab_demo.test_routes -v` — **2 passed**. Tests mock the Unix socket and verify same-origin plus no client-supplied context.
- Scoped TypeScript check: cached TypeScript 5.9.3, strict/no-emit, with a temporary minimal Lit type shim because this checkout has no `node_modules` — **passed** for `demo/src/realtime-voice.ts` and its `fire_event.ts` import. This is not the repository's complete frontend typecheck.
- Scoped esbuild bundle of `demo/src/realtime-voice.ts` with external packages — **passed** (15.6 KiB output, discarded to `/dev/null`).
- `PYTHONDONTWRITEBYTECODE=1 python3 habab/qa/issue1-realtime-demo-20261026/local-integration.py` — **PASS**. It served the real Flask demo routes and used a synthetic Unix bootstrap/control-socket peer plus a local synthetic WebSocket peer; Chromium loaded the actual candidate component bundle. It verified bootstrap, WebSocket auth, two PCM chunks for one item with a pause, and interruption truncating item `fixture-audio-item` at 130 ms (100 ms completed first chunk plus 30 ms of the second). It also verified dropping a cancelled function response, executing a completed function response, same-origin virtual-state persistence, another anonymous browser context retaining default state, and release of the mocked microphone track after Stop without another truncate. Output PCM was zero-valued synthetic data; the mocked microphone emitted no samples and the browser sent zero input audio frames. This fixture does **not** run the production relay or OpenAI.
- `git diff --check` — passed.
- Full frontend lint/typecheck/build remains unavailable: `pnpm`, repository `node_modules`, and installed Lit are absent; no dependency download was attempted. No full frontend build was run.

## Acceptance still pending

This is source, focused unit-test, and synthetic local browser evidence only. It was not installed or deployed; the live relay was not restarted, the demo page was not changed, and no OpenAI model request or device audio/microphone was used. The local fixture's media devices, WS peer, and store are mocks; it does not prove the production gateway-to-browser path. Therefore server key eligibility, live Nginx/WebSocket routing, OpenAI event compatibility, actual rendered UI, live virtual-state persistence, concurrent sessions, interruption on a real device, desktop/mobile/iframe behavior, and physical microphone/iOS remain unverified. habab.ru user documentation and human review are also pending; site files were outside this implementation scope.

OpenAI Docs were consulted for Realtime WebSocket auth, session updates, PCM events, output audio, and function-call event flow: [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations) and [WebSockets](https://developers.openai.com/api/docs/guides/voice-websockets).
