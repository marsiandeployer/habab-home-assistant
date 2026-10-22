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
    const voice = new AssistVoice({}, {}, () => {});
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
