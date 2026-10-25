import assert from "node:assert/strict";
import test from "node:test";
import { AssistVoice, BrowserGeminiVoice, bootstrapAfterStartCue } from "../src/habab_home/frontend/voice.js";

test("a finished subscription can be released twice without an unhandled rejection", async () => {
  const events = [];
  const voice = new AssistVoice({}, {}, (event) => events.push(event));
  let calls = 0;
  voice.unsubscribe = () => { calls++; return Promise.reject({ code: "not_found" }); };
  voice.releaseSubscription();
  voice.releaseSubscription();
  await new Promise(setImmediate);
  assert.equal(calls, 1);
  assert.deepEqual(events, []);
});

test("unexpected subscription errors remain visible", async () => {
  const events = [];
  const voice = new AssistVoice({}, {}, (event) => events.push(event));
  voice.unsubscribe = () => Promise.reject({ code: "connection_lost" });
  voice.close();
  await new Promise(setImmediate);
  assert.deepEqual(events, [{ type: "error", data: { code: "connection_lost" } }]);
});

test("leaving while microphone permission is pending stops the eventual stream", async () => {
  const windowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  let resolvePermission;
  let stops = 0;
  Object.defineProperty(globalThis, "window", { configurable: true, value: { isSecureContext: true } });
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: {
    mediaDevices: { getUserMedia: () => new Promise((resolve) => { resolvePermission = resolve; }) },
  } });
  try {
    const voice = new AssistVoice({}, {id: "test-pipeline", stt_engine: "test-stt"}, () => {});
    const pending = voice.start();
    voice.close();
    resolvePermission({ getTracks: () => [{ stop: () => { stops++; } }] });
    await pending;
    assert.equal(stops, 1);
    assert.equal(voice.context, undefined);
  } finally {
    if (windowDescriptor) Object.defineProperty(globalThis, "window", windowDescriptor);
    else delete globalThis.window;
    if (navigatorDescriptor) Object.defineProperty(globalThis, "navigator", navigatorDescriptor);
    else delete globalThis.navigator;
  }
});

test("speech input uses the requested Assist pipeline for STT only and releases the recording", async () => {
  const keys = ["window", "navigator", "AudioContext", "AudioWorkletNode", "WebSocket"];
  const descriptors = new Map(keys.map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  let stopped = 0;
  let disconnected = 0;
  let unsubscribed = 0;
  let subscription;
  class Context {
    constructor() { this.sampleRate = 16000; this.audioWorklet = {addModule: async () => {}}; }
    createMediaStreamSource() { return {connect() {}, disconnect() {disconnected++;}}; }
    async resume() {}
    async close() {}
  }
  class Recorder { constructor() { this.port = {onmessage: null}; } }
  Object.defineProperty(globalThis, "window", {configurable: true, value: {isSecureContext: true}});
  Object.defineProperty(globalThis, "navigator", {configurable: true, value: {mediaDevices: {
    getUserMedia: async () => ({getTracks: () => [{stop: () => {stopped++;}}]}),
  }}});
  Object.defineProperty(globalThis, "AudioContext", {configurable: true, value: Context});
  Object.defineProperty(globalThis, "AudioWorkletNode", {configurable: true, value: Recorder});
  Object.defineProperty(globalThis, "WebSocket", {configurable: true, value: {OPEN: 1}});
  try {
    const events = [];
    const hass = {connection: {
      socket: {readyState: 1, send() {}},
      subscribeMessage: async (callback, options) => {
        subscription = {callback, options};
        return async () => {unsubscribed++;};
      },
    }};
    const voice = new AssistVoice(hass, {id: "requested-pipeline", stt_engine: "stt"}, (event) => events.push(event));
    await voice.start();
    assert.deepEqual(subscription.options, {
      type: "assist_pipeline/run", pipeline: "requested-pipeline", start_stage: "stt", end_stage: "stt", input: {sample_rate: 16000},
    });
    subscription.callback({type: "stt-end", data: {stt_output: {text: "turn on the light"}}});
    subscription.callback({type: "run-end", data: null});
    await new Promise(setImmediate);
    assert.equal(stopped, 1);
    assert.equal(disconnected, 1);
    assert.equal(unsubscribed, 1);
    assert.deepEqual(events.map((event) => event.type), ["stt-end", "run-end"]);
  } finally {
    for (const [key, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test("spoken reply uses a separate TTS-only Assist stage without opening the microphone", async () => {
  const keys = ["Audio"];
  const descriptors = new Map(keys.map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  let played;
  let subscription;
  let unsubscribed = 0;
  class AudioStub {
    constructor(src) { this.src = src; played = this; }
    play() { this.didPlay = true; return Promise.resolve(); }
    pause() { this.didPause = true; }
    addEventListener() {}
  }
  Object.defineProperty(globalThis, "Audio", {configurable: true, value: AudioStub});
  try {
    const events = [];
    const hass = {hassUrl: (url) => `https://home.test${url}`, connection: {
      subscribeMessage: async (callback, options) => {
        subscription = {callback, options};
        return async () => {unsubscribed++;};
      },
    }};
    const voice = new AssistVoice(hass, {id: "speech-pipeline", tts_engine: "tts"}, (event) => events.push(event));
    const speech = voice.speak("Assistant response");
    await new Promise(setImmediate);
    assert.deepEqual(subscription.options, {
      type: "assist_pipeline/run", pipeline: "speech-pipeline", start_stage: "tts", end_stage: "tts", input: {text: "Assistant response"},
    });
    let settled = false;
    speech.then(() => { settled = true; });
    await new Promise(setImmediate);
    assert.equal(settled, false, "the TTS run must remain subscribed until its terminal event");
    subscription.callback({type: "tts-end", data: {tts_output: {url: "/api/tts_proxy/audio"}}});
    subscription.callback({type: "run-end", data: null});
    assert.equal(await speech, true);
    await new Promise(setImmediate);
    assert.equal(played.src, "https://home.test/api/tts_proxy/audio");
    assert.equal(played.didPlay, true);
    assert.equal(unsubscribed, 1);
    assert.deepEqual(events.map((event) => event.type), ["tts-end", "run-end"]);
    voice.close();
    assert.equal(played.didPause, true, "closing the chat must stop its active spoken reply");
  } finally {
    for (const [key, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test("closing during a pending spoken reply settles it and releases its subscription", async () => {
  let subscription;
  let unsubscribed = 0;
  const hass = {connection: {
    subscribeMessage: async (callback, options) => {
      subscription = {callback, options};
      return async () => {unsubscribed++;};
    },
  }};
  const voice = new AssistVoice(hass, {id: "speech-pipeline", tts_engine: "tts"}, () => {});
  const speech = voice.speak("Assistant response");
  await new Promise(setImmediate);
  voice.close();
  assert.equal(await speech, false);
  await new Promise(setImmediate);
  assert.equal(unsubscribed, 1);
  assert.equal(subscription.options.end_stage, "tts");
});

function recordingHarness(t) {
  const keys = ["window", "navigator", "AudioContext", "AudioWorkletNode", "WebSocket"];
  const descriptors = new Map(keys.map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const state = { sent: [], events: [], stopped: 0, unsubscribed: 0 };
  class Context {
    constructor() { this.sampleRate = 16000; this.audioWorklet = { addModule: async () => {} }; }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    async resume() {}
    async close() {}
  }
  class Recorder { constructor() { this.port = { onmessage: null }; state.recorder = this; } }
  const globals = {
    window: { isSecureContext: true },
    navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop: () => { state.stopped++; } }] }) } },
    AudioContext: Context, AudioWorkletNode: Recorder, WebSocket: { OPEN: 1 },
  };
  for (const [key, value] of Object.entries(globals)) Object.defineProperty(globalThis, key, { configurable: true, value });
  const hass = { connection: {
    socket: { readyState: 1, send: (frame) => state.sent.push(frame) },
    subscribeMessage: (callback) => {
      state.callback = callback;
      return new Promise((resolve) => { state.resolveSubscription = () => resolve(() => { state.unsubscribed++; }); });
    },
  } };
  state.voice = new AssistVoice(hass, { id: "privacy-pipeline", stt_engine: "stt" }, (event) => state.events.push(event));
  t.after(() => {
    state.voice.close();
    for (const [key, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  return state;
}

test("manual stop before a delayed runner drops buffered microphone audio and cancels the subscription", async (t) => {
  const h = recordingHarness(t);
  const starting = h.voice.start();
  await new Promise(setImmediate);
  const staleRecorderCallback = h.recorder.port.onmessage;
  staleRecorderCallback({ data: new Int16Array([101, 102]) });
  assert.equal(h.voice.pending.length, 1);
  h.voice.finishRecording();
  h.callback({ type: "run-start", data: { runner_data: { stt_binary_handler_id: 7 } } });
  h.callback({ type: "stt-start", data: {} });
  staleRecorderCallback({ data: new Int16Array([103]) });
  h.resolveSubscription();
  await starting;
  assert.deepEqual(h.sent, [], "neither stale buffered samples nor later samples may leave after stop");
  assert.equal(h.voice.pending.length, 0);
  assert.equal(h.stopped, 1);
  assert.equal(h.unsubscribed, 1);
});

test("VAD end invalidates stale capture and runner events while retaining the transcript", async (t) => {
  const h = recordingHarness(t);
  const starting = h.voice.start();
  await new Promise(setImmediate);
  h.resolveSubscription();
  await starting;
  const staleRecorderCallback = h.recorder.port.onmessage;
  h.callback({ type: "run-start", data: { runner_data: { stt_binary_handler_id: 7 } } });
  h.callback({ type: "stt-start", data: {} });
  staleRecorderCallback({ data: new Int16Array([101]) });
  assert.equal(h.sent.length, 1);
  h.callback({ type: "stt-vad-end", data: {} });
  h.callback({ type: "run-start", data: { runner_data: { stt_binary_handler_id: 8 } } });
  h.callback({ type: "stt-start", data: {} });
  staleRecorderCallback({ data: new Int16Array([102]) });
  h.voice.send(new Int16Array([103]));
  h.callback({ type: "stt-end", data: { stt_output: { text: "Привет" } } });
  assert.equal(h.sent.length, 1, "VAD end is a terminal microphone boundary");
  assert.equal(h.voice.ready, false);
  assert.equal(h.voice.handler, undefined);
  assert(h.events.some((event) => event.type === "stt-end" && event.data.stt_output.text === "Привет"));
});

test("manual stop sends one empty terminator and never forwards late microphone samples", async (t) => {
  const h = recordingHarness(t);
  const starting = h.voice.start();
  await new Promise(setImmediate);
  h.resolveSubscription();
  await starting;
  const staleRecorderCallback = h.recorder.port.onmessage;
  h.callback({ type: "run-start", data: { runner_data: { stt_binary_handler_id: 7 } } });
  h.callback({ type: "stt-start", data: {} });
  staleRecorderCallback({ data: new Int16Array([101]) });
  h.voice.finishRecording();
  h.voice.finishRecording();
  staleRecorderCallback({ data: new Int16Array([102]) });
  h.callback({ type: "stt-start", data: {} });
  assert.equal(h.sent.length, 2);
  assert.deepEqual([...h.sent[1]], [7], "the final frame is only the handler byte, with no audio payload");
  assert.equal(h.voice.pending.length, 0);
  assert.equal(h.stopped, 1);
});

test("browser start cue completes before bootstrap and stale bootstrap revokes its lease", async () => {
  const order = [];
  let current = true;
  const result = await bootstrapAfterStartCue({
    playCue: async (name) => { order.push(`${name}:begin`); await Promise.resolve(); order.push(`${name}:end`); },
    createSession: async () => { order.push("bootstrap"); return {session_id: "voice2_stale"}; },
    isCurrent: () => current,
    stopSession: async (id) => { order.push(`stop:${id}`); },
  });
  assert.deepEqual(order, ["start:begin", "start:end", "bootstrap"]);
  assert.equal(result.session_id, "voice2_stale");

  const staleOrder = [];
  const stale = await bootstrapAfterStartCue({
    playCue: async () => staleOrder.push("start:end"),
    createSession: async () => { staleOrder.push("bootstrap"); current = false; return {session_id: "voice2_late"}; },
    isCurrent: () => current,
    stopSession: async (id) => staleOrder.push(`stop:${id}`),
  });
  assert.equal(stale, undefined);
  assert.deepEqual(staleOrder, ["start:end", "bootstrap", "stop:voice2_late"]);
});

function browserVoiceHarness(t, {ttl = 180, onTurn, getUserMedia,
  expiresAt = Math.floor(Date.now() / 1000) + 60} = {}) {
  const keys = ["window", "navigator", "AudioContext", "AudioWorkletNode", "WebSocket"];
  const descriptors = new Map(keys.map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const state = {sockets: [], transcripts: [], events: [], turns: [], stops: [], cues: [], sources: []};
  class Context {
    constructor(options = {}) {
      this.sampleRate = options.sampleRate || 24000;
      this.currentTime = 0;
      this.destination = {};
      this.audioWorklet = {addModule: async () => {}};
    }
    createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
    createBuffer(channels, length, sampleRate) {
      return {duration: length / sampleRate, getChannelData: () => new Float32Array(length)};
    }
    createBufferSource() {
      const source = {connect() {}, start() { state.sources.push(this); }, stop() { this.onended?.(); }};
      return source;
    }
    createOscillator() { return {frequency: {}, connect() {}, disconnect() {}, start() {}, stop() {}}; }
    createGain() { return {gain: {setValueAtTime() {}, linearRampToValueAtTime() {}}, connect() {}, disconnect() {}}; }
    async resume() {}
    async close() {}
  }
  class Recorder { constructor() { this.port = {onmessage: null}; state.recorder = this; } }
  class Socket {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; state.sockets.push(this); }
    send(value) { this.sent.push(value); }
    close() { this.readyState = 3; }
  }
  Object.defineProperty(globalThis, "window", {configurable: true, value: {isSecureContext: true}});
  Object.defineProperty(globalThis, "navigator", {configurable: true, value: {mediaDevices: {
    getUserMedia: async () => {
      state.captureCount = (state.captureCount || 0) + 1;
      if (getUserMedia) return getUserMedia(state);
      return {getTracks: () => [{stop() { state.captureStopped = (state.captureStopped || 0) + 1; }}]};
    },
  }}});
  Object.defineProperty(globalThis, "AudioContext", {configurable: true, value: Context});
  Object.defineProperty(globalThis, "AudioWorkletNode", {configurable: true, value: Recorder});
  Object.defineProperty(globalThis, "WebSocket", {configurable: true, value: Socket});
  const voice = new BrowserGeminiVoice({
    client: "browser_v1", session_id: "voice2_test", value: "opaque-test-credential",
    setup: {model: "models/test"}, websocket_url: "wss://habab-voice.wpmix.net/gemini/ws",
    lease_ttl_seconds: ttl, expires_at: expiresAt,
    auth: {mode: "first_text_frame", type: "habab.voice.auth.v1"},
  }, {
    onEvent: (event) => state.events.push(event),
    onTurn: async (text, signal) => {
      state.turns.push({text, signal});
      return onTurn ? onTurn(text, signal) : {text: "Shared assistant result"};
    },
    onTranscript: async (event) => { state.transcripts.push(event); },
    onStop: async (id) => { state.stops.push(id); state.stopAt = Date.now(); },
    playCue: async (kind) => { state.cues.push(kind); },
  });
  t.after(() => {
    void voice.close();
    for (const [key, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  return {state, voice};
}

async function connectBrowserVoice(harness) {
  const started = harness.voice.start();
  const socket = harness.state.sockets[0];
  socket.readyState = 1;
  socket.onopen();
  assert.equal(JSON.parse(socket.sent[0]).type, "habab.voice.auth.v1");
  assert.equal(harness.state.captureCount || 0, 0, "capture waits for the relay setup acknowledgment");
  socket.onmessage({data: JSON.stringify({setupComplete: {}})});
  await started;
  return socket;
}

test("browser Gemini auth is the first WS frame and capture waits for setupComplete", async (t) => {
  const h = browserVoiceHarness(t);
  const socket = await connectBrowserVoice(h);
  const auth = JSON.parse(socket.sent[0]);
  assert.deepEqual(auth, {
    type: "habab.voice.auth.v1", credential: "opaque-test-credential", setup: {model: "models/test"},
  });
  assert.equal(socket.url, "wss://habab-voice.wpmix.net/gemini/ws", "credential is never added to the URL");
  assert.equal(h.state.captureCount, 1);
  h.state.recorder.port.onmessage({data: new Int16Array([1, -2])});
  const audio = JSON.parse(socket.sent[1]);
  assert.equal(audio.realtimeInput.audio.mimeType, "audio/pcm;rate=16000");
  assert.deepEqual([...Uint8Array.from(atob(audio.realtimeInput.audio.data), (char) => char.charCodeAt(0))], [1, 0, 254, 255]);
  await h.voice.finish();
  assert.equal(h.state.captureStopped, 1);
  assert.deepEqual(h.state.stops, ["voice2_test"]);
  assert.deepEqual(h.state.cues, ["end"]);
});

test("browser transcripts journal recognized fragments once and suppress canonical tool follow-up", async (t) => {
  const h = browserVoiceHarness(t);
  const socket = await connectBrowserVoice(h);
  socket.onmessage({data: JSON.stringify({
    voiceActivity: {type: "ACTIVITY_START"},
    serverContent: {inputTranscription: {text: "Проверь свет"}},
  })});
  socket.onmessage({data: JSON.stringify({voiceActivity: {type: "ACTIVITY_END"}})});
  await h.voice.receive({toolCall: {functionCalls: [{id: "call-1", name: "ask_home_assistant", args: {text: "Проверь свет"}}]},
    serverContent: {generationComplete: true}});
  await new Promise(setImmediate);
  const toolFrames = socket.sent.slice(1).map((frame) => JSON.parse(frame)).filter((frame) => frame.toolResponse);
  assert.equal(h.state.turns.length, 1, "a repeated provider call id cannot start a second shared turn");
  await h.voice.receive({toolCall: {functionCalls: [{id: "call-1", name: "ask_home_assistant", args: {text: "Проверь свет"}}]}});
  assert.equal(h.state.turns.length, 1);
  assert.equal(toolFrames.length, 1);
  assert.deepEqual(toolFrames[0].toolResponse.functionResponses[0], {
    id: "call-1", name: "ask_home_assistant", response: {answer: "Shared assistant result"}, scheduling: "WHEN_IDLE",
  });
  socket.onmessage({data: JSON.stringify({serverContent: {
    modelTurn: {parts: [{inlineData: {mimeType: "audio/pcm;rate=24000", data: "AQI="}}]},
    outputTranscription: {text: "Результат проверки"}, turnComplete: true,
  }})});
  await new Promise(setImmediate);
  for (const source of h.state.sources) source.onended?.();
  await new Promise(setImmediate);
  assert.equal(h.state.transcripts.length, 1, "the recognized input is journaled, while the canonical assistant follow-up is not duplicated");
  assert.equal(h.state.transcripts[0].role, "user");
  await h.voice.finish();
});

test("browser voice commits assistant transcription only after complete playback and drops interrupted output", async (t) => {
  const h = browserVoiceHarness(t);
  const socket = await connectBrowserVoice(h);
  await h.voice.receive({voiceActivity: {type: "ACTIVITY_START"}, serverContent: {inputTranscription: {text: "Привет"}}});
  await h.voice.receive({voiceActivity: {type: "ACTIVITY_END"}});
  await h.voice.receive({serverContent: {
    modelTurn: {parts: [
      {inlineData: {mimeType: "audio/pcm;rate=24000", data: "AQI="}},
      {inlineData: {mimeType: "audio/pcm;rate=24000", data: "AwQ="}},
    ]},
    outputTranscription: {text: "Здравствуйте"}, turnComplete: true,
  }});
  await new Promise(setImmediate);
  assert.equal(h.state.transcripts.filter((entry) => entry.role === "assistant").length, 0);
  assert.equal(h.state.sources.length, 2, "all PCM parts from one Gemini frame enter the playback queue");
  for (const source of h.state.sources) source.onended?.();
  await new Promise(setImmediate);
  assert.deepEqual(h.state.transcripts.map((entry) => entry.role), ["user", "assistant"]);
  assert.notEqual(h.state.transcripts[0].event_id, h.state.transcripts[1].event_id);
  await h.voice.receive({serverContent: {modelTurn: {parts: [{inlineData: {mimeType: "audio/pcm;rate=24000", data: "AQI="}}]}, outputTranscription: {text: "Старый ответ"}}});
  await h.voice.receive({serverContent: {interrupted: true}});
  for (const source of h.state.sources) source.onended?.();
  await h.voice.receive({serverContent: {turnComplete: true}});
  assert.equal(h.state.transcripts.filter((entry) => entry.role === "assistant").length, 1);
  assert(socket.readyState === 1);
  await h.voice.finish();
});

test("browser voice lease expiry gates capture and stops the backend lease", async (t) => {
  const h = browserVoiceHarness(t, {ttl: 1});
  const startedAt = Date.now();
  const socket = await connectBrowserVoice(h);
  await new Promise((resolve) => setTimeout(resolve, 930));
  assert.equal(h.voice.accepting, false);
  assert.deepEqual(h.state.stops, ["voice2_test"]);
  assert(h.state.stopAt < startedAt + 1000, "stop begins before the server-reported lease deadline");
  assert(h.state.events.some((event) => event.type === "expired"));
  assert.deepEqual(h.state.cues, ["end"]);
  assert.equal(socket.readyState, 3);
});

test("closing while browser microphone permission is pending stops the eventual stream", async (t) => {
  let grantPermission;
  const h = browserVoiceHarness(t, {getUserMedia: async () => new Promise((resolve) => { grantPermission = resolve; })});
  const started = h.voice.start();
  const socket = h.state.sockets[0];
  socket.readyState = 1;
  socket.onopen();
  socket.onmessage({data: JSON.stringify({setupComplete: {}})});
  await new Promise(setImmediate);
  const closing = h.voice.close();
  grantPermission({getTracks: () => [{stop() { h.state.captureStopped = (h.state.captureStopped || 0) + 1; }}]});
  await assert.rejects(started);
  await closing;
  assert.equal(h.state.captureStopped, 1);
  assert.equal(socket.sent.length, 1, "closing during permission never sends audio");
  assert.deepEqual(h.state.stops, ["voice2_test"]);
});

test("credential expiry before setupComplete tears down the bootstrap lease", async (t) => {
  const h = browserVoiceHarness(t, {ttl: 180, expiresAt: Math.floor(Date.now() / 1000) + 1});
  const started = h.voice.start();
  const socket = h.state.sockets[0];
  socket.readyState = 1;
  socket.onopen();
  await assert.rejects(started, (error) => error?.code === "timeout");
  await new Promise(setImmediate);
  assert.equal(h.state.captureCount || 0, 0);
  assert.deepEqual(h.state.stops, ["voice2_test"]);
  assert(h.state.events.some((event) => event.type === "error" && event.code === "timeout"));
});

test("browser bootstrap accepts relay Unix seconds and rejects malformed or expired expiry values", async (t) => {
  const h = browserVoiceHarness(t);
  assert.equal(typeof h.voice.session.expires_at, "number", "fixture matches relay JSON Unix seconds");
  const badExpiries = [
    true,
    "2026-10-25T12:00:00Z",
    Number.NaN,
    Math.floor(Date.now() / 1000) - 1,
  ];
  for (const expiresAt of badExpiries) {
    h.voice.session.expires_at = expiresAt;
    await assert.rejects(h.voice.start(), (error) => error?.code === "browserVoiceFailed");
    assert.equal(h.state.sockets.length, 0, "invalid expiry is rejected before opening a relay socket");
  }
});

test("browser end gates capture synchronously and plays END only after lease stop resolves", async (t) => {
  const h = browserVoiceHarness(t);
  const socket = await connectBrowserVoice(h);
  let finishStop;
  const stopStarted = [];
  h.voice.onStop = async (id) => {
    stopStarted.push(id);
    await new Promise((resolve) => { finishStop = resolve; });
  };
  const staleCapture = h.state.recorder.port.onmessage;
  const ending = h.voice.finish();
  assert.equal(h.voice.accepting, false);
  assert.equal(h.state.captureStopped, 1);
  assert.equal(socket.readyState, 3);
  staleCapture({data: new Int16Array([3, 4])});
  assert.equal(socket.sent.length, 1, "no PCM can leave after END begins");
  assert.deepEqual(stopStarted, ["voice2_test"]);
  assert.deepEqual(h.state.cues, [], "END cue waits until the stop request settles");
  finishStop();
  await ending;
  assert.deepEqual(h.state.cues, ["end"]);
});

test("a barge-in makes a late shared tool result silent while explicit tool cancellation aborts it", async (t) => {
  let resolveTurn;
  const h = browserVoiceHarness(t, {onTurn: (_text, signal) => {
    signal.addEventListener("abort", () => {});
    return new Promise((resolve) => { resolveTurn = resolve; });
  }});
  const socket = await connectBrowserVoice(h);
  await h.voice.receive({toolCall: {functionCalls: [{id: "call-barge", name: "ask_home_assistant", args: {text: "Найди устройство"}}]}});
  await h.voice.receive({voiceActivity: {type: "ACTIVITY_START"}});
  resolveTurn({text: "Готово"});
  await new Promise(setImmediate);
  let frames = socket.sent.slice(1).map((frame) => JSON.parse(frame)).filter((frame) => frame.toolResponse);
  assert.equal(frames.length, 1);
  assert.equal(frames[0].toolResponse.functionResponses[0].scheduling, "SILENT");

  let resolveCancelled;
  h.voice.onTurn = (_text, signal) => new Promise((resolve) => {
    resolveCancelled = resolve;
    h.cancelSignal = signal;
  });
  await h.voice.receive({toolCall: {functionCalls: [{id: "call-cancel", name: "ask_home_assistant", args: {text: "Выключи свет"}}]}});
  await h.voice.receive({toolCallCancellation: {ids: ["call-cancel"]}});
  assert.equal(h.cancelSignal.aborted, true);
  resolveCancelled({text: "Поздний результат"});
  await new Promise(setImmediate);
  frames = socket.sent.slice(1).map((frame) => JSON.parse(frame)).filter((frame) => frame.toolResponse);
  assert.equal(frames.length, 1, "cancelled tool calls never receive a late provider response");
  await h.voice.finish();
});
