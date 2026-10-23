import assert from "node:assert/strict";
import test from "node:test";
import { AssistVoice } from "../src/habab_home/frontend/voice.js";

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
