"""Local bootstrap-to-demo fixture with synthetic PCM; no OpenAI or device audio."""
import asyncio
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

from flask import Flask, Response, jsonify
from werkzeug.serving import WSGIRequestHandler, make_server
import websockets

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'habab'))
sys.path.insert(0, str(REPO / 'habab' / 'android' / 'relay'))
from habab_demo.routes import HADemoSessionInterface, ha_demo_bp  # noqa: E402


class FixtureStore:
    def __init__(self):
        self.states = {}
        self.lock = threading.Lock()
        self.reads = []

    def get_state(self, session_hash, seed):
        with self.lock:
            result = json.loads(json.dumps(self.states.get(session_hash, {})))
            self.reads.append({'session_hash': session_hash, 'states': result})
            return result

    def update_state(self, session_hash, updates, seed):
        with self.lock:
            saved = self.states.setdefault(session_hash, {})
            saved.update(json.loads(json.dumps(updates)))


class QuietRequestHandler(WSGIRequestHandler):
    def log(self, type, message, *args):
        pass


def build_browser_bundle(output):
    source = (REPO / 'demo' / 'src' / 'realtime-voice.ts').read_text()
    required_source_contract = (
        'aria-describedby=${this._running ? "" : "voice-privacy"}',
        'title=${actionLabel}',
        '@click=${this._running ? this._stopFromClick : this._start}',
        '<details>',
        '<summary aria-label="Голосовой помощник демо-квартиры"',
        'role="status" aria-live="polite"',
        'width: auto;',
        'inset-block-start: max(16px, env(safe-area-inset-top));',
    )
    missing = [contract for contract in required_source_contract if contract not in source]
    if missing:
        raise RuntimeError('voice UI source contract missing: ' + ', '.join(missing))
    root = output.parent
    lit_stub = root / 'lit-stub.mjs'
    decorators_stub = root / 'decorators-stub.mjs'
    lit_stub.write_text('''
export class LitElement extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: "open" }); }
  disconnectedCallback() {}
}
export const css = (strings) => strings.join("");
export const html = (strings, ...values) => ({ strings, values });
''')
    decorators_stub.write_text('''
export const customElement = (tag) => (constructor) => customElements.define(tag, constructor);
export const state = () => () => {};
''')
    result = subprocess.run([
        '/root/but/node_modules/esbuild/bin/esbuild',
        str(REPO / 'demo' / 'src' / 'realtime-voice.ts'),
        '--bundle', '--format=iife', '--target=es2022',
        f'--alias:lit={lit_stub}', f'--alias:lit/decorators={decorators_stub}',
        f'--outfile={output}',
    ], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('scoped esbuild failed: ' + result.stderr.strip())


async def run_fixture():
    store = FixtureStore()
    browser_frames = []
    function_output_received = threading.Event()
    relay_sessions = 0
    bootstrap_count = 0
    token = 'synthetic-one-use-demo-grant'

    async def control_client(reader, writer):
        nonlocal bootstrap_count
        try:
            request = json.loads(await asyncio.wait_for(reader.readline(), 2))
            assert request['purpose'] == 'demo_openai_bootstrap'
            assert request['upstream_key'] == 'synthetic-openai-key'
            assert len(request['session_hash']) == 64
            bootstrap_count += 1
            writer.write((json.dumps({'ready': True, 'value': token,
                'websocket_url': websocket_url}) + '\n').encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def relay_ws(socket):
        nonlocal relay_sessions
        relay_sessions += 1
        auth = json.loads(await asyncio.wait_for(socket.recv(), 3))
        if auth != {'type': 'habab.demo.openai.auth.v1', 'token': token}:
            await socket.close(code=1008, reason='invalid fixture grant')
            return
        await socket.send(json.dumps({'type': 'session.updated'}))
        await socket.send(json.dumps({'type': 'response.created', 'response': {'id': 'fixture-audio-response'}}))
        await socket.send(json.dumps({'type': 'response.output_audio.delta',
            'item_id': 'fixture-audio-item', 'content_index': 0,
            'response_id': 'fixture-audio-response',
            'delta': base64.b64encode(bytes(4800)).decode('ascii')}))
        await socket.send(json.dumps({'type': 'fixture.after_first_chunk'}))
        await socket.send(json.dumps({'type': 'response.output_audio.delta',
            'item_id': 'fixture-audio-item', 'content_index': 0,
            'response_id': 'fixture-audio-response',
            'delta': base64.b64encode(bytes(4800)).decode('ascii')}))
        await socket.send(json.dumps({'type': 'fixture.before_interrupt'}))
        await socket.send(json.dumps({'type': 'input_audio_buffer.speech_started'}))
        await socket.send(json.dumps({'type': 'response.done', 'response': {
            'id': 'fixture-audio-response', 'status': 'cancelled', 'output': [],
        }}))
        await socket.send(json.dumps({'type': 'response.created', 'response': {'id': 'fixture-cancelled-response'}}))
        await socket.send(json.dumps({'type': 'response.output_item.added',
            'response_id': 'fixture-cancelled-response', 'item': {
                'type': 'function_call', 'id': 'fixture-cancelled-item',
                'call_id': 'fixture-cancelled-call', 'name': 'set_demo_light',
            }}))
        await socket.send(json.dumps({'type': 'response.function_call_arguments.done',
            'response_id': 'fixture-cancelled-response', 'item_id': 'fixture-cancelled-item',
            'call_id': 'fixture-cancelled-call', 'name': 'set_demo_light',
            'arguments': json.dumps({'entity_id': 'light.kitchen_worktop', 'state': 'on'})}))
        await socket.send(json.dumps({'type': 'response.done', 'response': {
            'id': 'fixture-cancelled-response', 'status': 'cancelled', 'output': [{
                'type': 'function_call', 'id': 'fixture-cancelled-item',
                'call_id': 'fixture-cancelled-call', 'name': 'set_demo_light',
                'arguments': json.dumps({'entity_id': 'light.kitchen_worktop', 'state': 'on'}),
            }],
        }}))
        await socket.send(json.dumps({'type': 'response.created', 'response': {'id': 'fixture-success-response'}}))
        await socket.send(json.dumps({'type': 'response.output_item.added',
            'response_id': 'fixture-success-response', 'item': {
                'type': 'function_call', 'id': 'fixture-success-item',
                'call_id': 'fixture-success-call', 'name': 'set_demo_light',
            }}))
        success_arguments = json.dumps({'entity_id': 'light.living_room_floor_lamp', 'state': 'off'})
        await socket.send(json.dumps({'type': 'response.function_call_arguments.done',
            'response_id': 'fixture-success-response', 'item_id': 'fixture-success-item',
            'call_id': 'fixture-success-call', 'name': 'set_demo_light',
            'arguments': success_arguments}))
        await socket.send(json.dumps({'type': 'response.done', 'response': {
            'id': 'fixture-success-response', 'status': 'completed', 'output': [{
                'type': 'function_call', 'id': 'fixture-success-item',
                'call_id': 'fixture-success-call', 'name': 'set_demo_light',
                'arguments': success_arguments,
            }],
        }}))
        try:
            async for message in socket:
                event = json.loads(message)
                browser_frames.append(event)
                if (event.get('type') == 'conversation.item.create'
                        and event.get('item', {}).get('type') == 'function_call_output'):
                    function_output_received.set()
        except websockets.ConnectionClosed:
            pass

    control_server = None
    relay_server = None
    websocket_url = ''

    with tempfile.TemporaryDirectory(prefix='habab-issue1-local-') as temp:
        root = Path(temp)
        bundle = root / 'voice.js'
        build_browser_bundle(bundle)

        app = Flask(__name__)
        app.secret_key = 'synthetic-issue1-fixture-secret'
        app.config.update(
            TESTING=True,
            OPENAI_API_KEY='synthetic-openai-key',
            HA_DEMO_STORE=store,
            HA_DEMO_REALTIME_CONTROL_SOCKET=str(root / 'control.sock'),
        )
        app.session_interface = HADemoSessionInterface()
        app.register_blueprint(ha_demo_bp)

        @app.get('/__fixture')
        def fixture_page():
            return Response('''<!doctype html><html><body>
<style>
html, body { margin: 0; }
#dashboard-probe { position: fixed; z-index: 0; inset: 0; pointer-events: auto; }
habab-setup-chat { position: fixed; z-index: 10; inset: 0 0 0 auto; width: max(420px, 50vw); height: 100dvh; pointer-events: none; }
habab-setup-chat .drawer { position: absolute; inset: 0; display: flex; flex-direction: column; background: white; pointer-events: auto; }
habab-setup-chat footer { margin-top: auto; min-height: 100px; }
@media (max-width: 720px) {
  habab-setup-chat { inset: 0; z-index: 1000; width: 100vw; }
  habab-setup-chat .drawer { inset: 50dvh 0 0; }
  habab-setup-chat[data-expanded] .drawer { inset-block-start: 0; }
}
</style>
<div id="dashboard-probe">Dashboard</div>
<habab-setup-chat data-assistant-open><div class="drawer"><main></main><footer id="chat-composer">Chat composer</footer></div></habab-setup-chat>
<script>
customElements.define("ha-demo", class extends HTMLElement {});
window.__micStarts = 0;
window.__trackStops = 0;
window.__processorCount = 0;
window.__wsTypes = [];
window.__fetchTrace = [];
const nativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const response = await nativeFetch(...args);
  let body;
  try { body = await response.clone().json(); } catch (_) { body = null; }
  window.__fetchTrace.push({ url: String(args[0]), status: response.status, body });
  return response;
};
const nativeAddEventListener = WebSocket.prototype.addEventListener;
WebSocket.prototype.addEventListener = function(type, listener, options) {
  if (type !== "message") return nativeAddEventListener.call(this, type, listener, options);
  return nativeAddEventListener.call(this, type, (event) => {
    try {
      const parsed = JSON.parse(event.data);
      window.__wsTypes.push(parsed.type);
      if (parsed.type === "fixture.after_first_chunk" && window.__audioContext) {
        window.__audioContext.currentTime = 0.14;
        window.__audioSources[0]?.finish();
      }
      if (parsed.type === "fixture.before_interrupt" && window.__audioContext)
        window.__audioContext.currentTime = 0.20;
    } catch (_) {}
    listener.call(this, event);
  }, options);
};
Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: {
  getUserMedia: async () => {
    window.__micStarts++;
    return { getTracks: () => [{ stop: () => window.__trackStops++ }] };
  }
} });
window.AudioContext = class {
  constructor() {
    this.sampleRate = 24000; this.currentTime = 0; this.destination = {};
    window.__audioContext = this;
  }
  async resume() {}
  async close() {}
  createMediaStreamSource() { return { connect() {} }; }
  createScriptProcessor() {
    window.__processorCount++;
    return { onaudioprocess: null, connect() {}, disconnect() {} };
  }
  createGain() { return { gain: { value: 1 }, connect() {} }; }
  createBuffer(_channels, length, sampleRate) {
    const channel = new Float32Array(length);
    return { duration: length / sampleRate, getChannelData: () => channel };
  }
  createBufferSource() {
    const source = {
      listeners: {},
      connect() {}, start() {}, stop() { window.__audioStops++; },
      addEventListener(type, listener) { this.listeners[type] = listener; },
      finish() { this.listeners.ended?.(); },
    };
    window.__audioSources.push(source);
    return source;
  }
};
window.__audioStops = 0;
window.__audioSources = [];
</script>
<script src="/__fixture/voice.js"></script>
<script>
customElements.whenDefined("ha-demo-realtime-voice").then(() => {
  const voice = document.querySelector("ha-demo-realtime-voice");
  const constructor = customElements.get("ha-demo-realtime-voice");
  const privacy = "Микрофон включится после нажатия. Звук проходит через сервер Habab в OpenAI.";
  voice.shadowRoot.innerHTML = `<style>${constructor.styles}</style>
    <section aria-label="Голосовой помощник демо-квартиры">
      <div class="controls">
        <button id="voice-action" aria-describedby="voice-privacy" title="${privacy}" aria-label="Начать разговор">
          <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="12" rx="3"></rect><path d="M5 11a7 7 0 0 0 14 0M12 18v3m-4 0h8"></path></svg>
          Начать разговор
        </button>
        <details><summary aria-label="Голосовой помощник демо-квартиры" title="Голосовой помощник демо-квартиры">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"></circle><path d="M12 11v5m0-8h.01"></path></svg>
        </summary><div class="details-panel"><strong>Голосовой помощник выключен</strong>
          <p class="privacy">${privacy}</p></div></details>
      </div>
      <span class="visually-hidden" id="voice-privacy">${privacy}</span>
      <span class="visually-hidden" role="status" aria-live="polite" aria-atomic="true">Голосовой помощник выключен</span>
    </section>`;
  window.__voice = voice;
  const action = voice.shadowRoot.querySelector("#voice-action");
  action.addEventListener("click", () => { void voice._start(); });
  window.__syncVoiceControl = () => {
    if (!voice._running) return;
    action.setAttribute("aria-label", "Остановить");
    action.title = "Остановить";
    action.textContent = "Остановить";
    action.onclick = () => voice._stopFromClick();
  };
});
</script>
</body></html>''')

        @app.get('/__fixture/voice.js')
        def fixture_bundle():
            return Response(bundle.read_text(), mimetype='text/javascript')

        @app.get('/__fixture/complete')
        def fixture_complete():
            return jsonify(complete=function_output_received.is_set())

        wsgi = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietRequestHandler)
        web_thread = threading.Thread(target=wsgi.serve_forever, daemon=True)
        web_thread.start()
        web_port = wsgi.server_port
        origin = f'http://127.0.0.1:{web_port}'
        control_server = await asyncio.start_unix_server(control_client, path=str(root / 'control.sock'))
        relay_server = await websockets.serve(relay_ws, '127.0.0.1', 0)
        relay_port = relay_server.sockets[0].getsockname()[1]
        websocket_url = f'ws://127.0.0.1:{relay_port}/openai/demo/ws'
        try:
            playwright_runner = root / 'browser-check.cjs'
            playwright_runner.write_text('''
const { chromium } = require('/root/but/node_modules/playwright');
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: '/snap/bin/chromium',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'] });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const page = await context.newPage();
    await page.goto(process.argv[2] + '/__fixture', { waitUntil: 'networkidle' });
    await page.waitForFunction(() => document.querySelector('ha-demo-realtime-voice'));
    await page.waitForFunction(() => document.querySelector('ha-demo-realtime-voice')
      ?.shadowRoot?.querySelector('#voice-action'));
    const startButton = page.getByRole('button', { name: 'Начать разговор' });
    if (await startButton.count() !== 1 || !(await startButton.isVisible()))
      throw new Error('compact voice start button is not accessible');
    const privacyDescription = await startButton.evaluate((button) => {
      const id = button.getAttribute('aria-describedby');
      return id ? button.getRootNode().getElementById(id)?.textContent : null;
    });
    if (!privacyDescription?.includes('Микрофон включится после нажатия')
        || !privacyDescription.includes('OpenAI'))
      throw new Error('voice start button lacks its privacy description');
    const summary = page.locator('ha-demo-realtime-voice').locator('summary');
    await summary.focus();
    await page.keyboard.press('Enter');
    const disclosureOpened = await page.locator('ha-demo-realtime-voice').locator('details')
      .evaluate((details) => details.open);
    const privacyVisible = await page.locator('ha-demo-realtime-voice').locator('.details-panel .privacy').isVisible();
    if (!disclosureOpened || !privacyVisible)
      throw new Error('keyboard disclosure does not reveal voice privacy details');
    await page.keyboard.press('Enter');
    const desktopCompact = await page.evaluate(() => {
      const voice = document.querySelector('ha-demo-realtime-voice').getBoundingClientRect();
      return { height: voice.height,
        dashboardHit: document.elementFromPoint(500, 400)?.id,
        composerHit: (() => {
          const composer = document.querySelector('#chat-composer').getBoundingClientRect();
          return document.elementFromPoint(composer.left + composer.width / 2,
            composer.top + composer.height / 2)?.id;
        })() };
    });
    if (desktopCompact.height > 52 || desktopCompact.dashboardHit !== 'dashboard-probe'
        || desktopCompact.composerHit !== 'chat-composer')
      throw new Error('desktop compact control blocks the dashboard or chat: ' + JSON.stringify(desktopCompact));
    const desktopLayout = await page.evaluate(() => {
      document.documentElement.dir = 'rtl';
      const voice = document.querySelector('ha-demo-realtime-voice').getBoundingClientRect();
      const composer = document.querySelector('#chat-composer').getBoundingClientRect();
      document.documentElement.dir = 'ltr';
      return { voice: { left: voice.left, right: voice.right, top: voice.top, bottom: voice.bottom },
        composer: { left: composer.left, right: composer.right, top: composer.top, bottom: composer.bottom } };
    });
    if (desktopLayout.voice.right > desktopLayout.composer.left
        && desktopLayout.voice.left < desktopLayout.composer.right
        && desktopLayout.voice.bottom > desktopLayout.composer.top
        && desktopLayout.voice.top < desktopLayout.composer.bottom)
      throw new Error('desktop voice panel overlaps chat composer: ' + JSON.stringify(desktopLayout));
    await page.setViewportSize({ width: 390, height: 844 });
    const mobileCompact = await page.evaluate(() => {
      const voice = document.querySelector('ha-demo-realtime-voice').getBoundingClientRect();
      return { height: voice.height,
        dashboardHit: document.elementFromPoint(300, 300)?.id,
        composerHit: (() => {
          const composer = document.querySelector('#chat-composer').getBoundingClientRect();
          return document.elementFromPoint(composer.left + composer.width / 2,
            composer.top + composer.height / 2)?.id;
        })() };
    });
    if (mobileCompact.height > 52 || mobileCompact.dashboardHit !== 'dashboard-probe'
        || mobileCompact.composerHit !== 'chat-composer')
      throw new Error('mobile compact control blocks the dashboard or chat: ' + JSON.stringify(mobileCompact));
    const mobileLayout = await page.evaluate(() => {
      const voice = document.querySelector('ha-demo-realtime-voice').getBoundingClientRect();
      const composer = document.querySelector('#chat-composer').getBoundingClientRect();
      return { voice: { left: voice.left, right: voice.right, top: voice.top, bottom: voice.bottom },
        composer: { left: composer.left, right: composer.right, top: composer.top, bottom: composer.bottom } };
    });
    if (mobileLayout.voice.right > mobileLayout.composer.left
        && mobileLayout.voice.left < mobileLayout.composer.right
        && mobileLayout.voice.bottom > mobileLayout.composer.top
        && mobileLayout.voice.top < mobileLayout.composer.bottom)
      throw new Error('mobile voice panel overlaps chat composer: ' + JSON.stringify(mobileLayout));
    const expandedMobile = await page.evaluate(() => {
      const voice = document.querySelector('ha-demo-realtime-voice');
      const chat = document.querySelector('habab-setup-chat');
      chat.setAttribute('data-expanded', '');
      return { voiceZ: Number(getComputedStyle(voice).zIndex), chatZ: Number(getComputedStyle(chat).zIndex) };
    });
    if (expandedMobile.voiceZ >= expandedMobile.chatZ)
      throw new Error('expanded mobile chat does not stack above voice panel: ' + JSON.stringify(expandedMobile));
    await page.setViewportSize({ width: 1280, height: 800 });
    const beforeStart = await page.evaluate(() => window.__micStarts);
    if (beforeStart !== 0) throw new Error('microphone requested before explicit start');
    await page.getByRole('button', { name: 'Начать разговор' }).click();
    const deadline = Date.now() + 10000;
    let saved = false;
    while (!saved && Date.now() < deadline) {
      saved = await page.evaluate(async () => {
        const response = await fetch('/api/ha-demo/state');
        const payload = await response.json();
        return response.ok && payload.states['light.living_room_floor_lamp']?.state === 'off';
      });
      if (!saved) await new Promise((resolve) => setTimeout(resolve, 50));
    }
    if (!saved) throw new Error('demo state did not reach expected value');
    const outputDeadline = Date.now() + 3000;
    let outputReceived = false;
    while (!outputReceived && Date.now() < outputDeadline) {
      outputReceived = await page.evaluate(async () =>
        (await (await fetch('/__fixture/complete')).json()).complete);
      if (!outputReceived) await new Promise((resolve) => setTimeout(resolve, 50));
    }
    if (!outputReceived) throw new Error('fixture relay did not receive function result');
    await page.evaluate(() => window.__syncVoiceControl());
    const active = await page.evaluate(() => ({ micStarts: window.__micStarts,
      processors: window.__processorCount, trackStops: window.__trackStops,
      audioStops: window.__audioStops,
      status: window.__voice._status, hasStream: Boolean(window.__voice._stream),
      hasContext: Boolean(window.__voice._context), socketState: window.__voice._socket?.readyState,
      wsTypes: window.__wsTypes, fetchTrace: window.__fetchTrace }));
    if (active.micStarts !== 1 || active.trackStops !== 0)
      throw new Error('synthetic microphone permission did not begin only after click: ' + JSON.stringify(active));
    if (active.processors !== 1 || active.audioStops !== 1
        || !active.wsTypes.includes('input_audio_buffer.speech_started'))
      throw new Error('interruption did not stop synthetic partial playback: ' + JSON.stringify(active));
    await page.getByRole('button', { name: 'Остановить' }).click();
    await page.waitForFunction(() => window.__trackStops === 1, null, { timeout: 3000 });
    const stopped = await page.evaluate(() => ({ micStarts: window.__micStarts,
      trackStops: window.__trackStops, running: window.__voice._running }));
    if (stopped.trackStops !== 1 || stopped.running !== false)
      throw new Error('stop did not release synthetic microphone track: ' + JSON.stringify(stopped));

    const isolatedContext = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const isolatedPage = await isolatedContext.newPage();
    await isolatedPage.goto(process.argv[2] + '/__fixture', { waitUntil: 'networkidle' });
    const isolatedState = await isolatedPage.evaluate(async () => {
      const response = await fetch('/api/ha-demo/state');
      const payload = await response.json();
      return payload.states['light.living_room_floor_lamp'].state;
    });
    if (isolatedState !== 'on') throw new Error('another anonymous session saw persisted voice state');
    process.stdout.write(JSON.stringify({ beforeStart, active, stopped, isolatedState }) + '\\n');
    await isolatedContext.close();
    await context.close();
  } finally {
    await browser.close();
  }
})().catch((error) => { process.stderr.write(String(error.stack || error) + '\\n'); process.exit(1); });
''')
            result = await asyncio.to_thread(
                subprocess.run,
                ['node', str(playwright_runner), origin],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode:
                raise RuntimeError('browser fixture failed: ' + result.stderr.strip()
                    + '; bootstrap=' + str(bootstrap_count) + '; relay=' + str(relay_sessions)
                    + '; frames=' + repr(browser_frames) + '; stored=' + repr(store.states)
                    + '; reads=' + repr(store.reads))
            if bootstrap_count != 1 or relay_sessions != 1:
                raise AssertionError('fixture did not complete exactly one bootstrap/WebSocket session')
            if any(frame.get('type') == 'input_audio_buffer.append' for frame in browser_frames):
                raise AssertionError('fixture unexpectedly sent synthetic microphone audio')
            outputs = [frame for frame in browser_frames
                       if frame.get('type') == 'conversation.item.create'
                       and frame.get('item', {}).get('type') == 'function_call_output']
            if (len(outputs) != 1 or outputs[0]['item'].get('call_id') != 'fixture-success-call'
                    or json.loads(outputs[0]['item']['output']) != {'saved': True}):
                raise AssertionError('browser did not return successful function result: '
                                     + repr(browser_frames) + '; browser=' + result.stdout.strip()
                                     + '; stored=' + repr(store.states) + '; reads=' + repr(store.reads))
            if not store.states or not any(
                    values.get('light.living_room_floor_lamp', {}).get('state') == 'off'
                    for values in store.states.values()):
                raise AssertionError('virtual state was not saved through the Flask demo API')
            if any('light.kitchen_worktop' in values for values in store.states.values()):
                raise AssertionError('cancelled response executed a virtual action')
            truncations = [frame for frame in browser_frames
                           if frame.get('type') == 'conversation.item.truncate']
            if truncations != [{'type': 'conversation.item.truncate', 'item_id': 'fixture-audio-item',
                                'content_index': 0, 'audio_end_ms': 130}]:
                raise AssertionError('partial playback was not truncated at the heard offset: '
                                     + repr(truncations))
            print(json.dumps({
                'result': 'PASS', 'bootstrap_count': bootstrap_count,
                'relay_websocket_sessions': relay_sessions,
                'virtual_state_sessions': len(store.states),
                'function_output_received': True, 'microphone_audio_frames': 0,
                'browser': result.stdout.strip(),
            }, sort_keys=True))
        finally:
            relay_server.close()
            await relay_server.wait_closed()
            control_server.close()
            await control_server.wait_closed()
            wsgi.shutdown()
            web_thread.join(timeout=3)


if __name__ == '__main__':
    asyncio.run(run_fixture())
