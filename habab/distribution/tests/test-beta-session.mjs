import assert from "node:assert/strict";
import test from "node:test";

let requestAssistantSession;

test("requestAssistantSession handles beta activation only for provider_required", async (t) => {
  // Import without a document and keep setup-chat's browser bootstrap dormant.
  const previousDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  const previousInterval = Object.getOwnPropertyDescriptor(globalThis, "setInterval");
  const previousCustomElements = Object.getOwnPropertyDescriptor(globalThis, "customElements");
  Object.defineProperty(globalThis, "document", {configurable: true, value: undefined});
  Object.defineProperty(globalThis, "setInterval", {configurable: true, value: () => 0});
  Object.defineProperty(globalThis, "customElements", {configurable: true, value: {get: () => undefined}});
  t.after(() => {
    for (const [key, descriptor] of [["document", previousDocument], ["setInterval", previousInterval], ["customElements", previousCustomElements]]) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  ({requestAssistantSession} = await import(new URL("../src/habab_home/frontend/setup-chat.js?beta-session-test", import.meta.url)));

  const current = () => true;
  const options = {isCurrent: current, onActivating() {}};

  await t.test("an existing session is returned without enrollment", async () => {
    const result = {session_id: "already-connected"};
    const calls = [];
    const hass = {callWS: async (request) => { calls.push(request); return result; }};
    assert.equal(await requestAssistantSession(hass, options), result);
    assert.deepEqual(calls, [{type: "habab_home/agent/session", new: false}]);
  });

  await t.test("provider_required activates once, retries once, and preserves fresh", async () => {
    const calls = [];
    let activations = 0;
    let activating = 0;
    const session = {session_id: "new-session"};
    const hass = {callWS: async (request) => {
      calls.push(request);
      if (request.type === "habab_home/agent/session" && calls.filter((item) => item.type === request.type).length === 1) throw {code: "provider_required"};
      if (request.type === "habab_home/beta/activate") { activations++; return {status: "ready"}; }
      return session;
    }};
    const result = await requestAssistantSession(hass, {fresh: true, isCurrent: current, onActivating: () => activating++});
    assert.equal(result, session);
    assert.equal(activating, 1);
    assert.equal(activations, 1);
    assert.deepEqual(calls, [
      {type: "habab_home/agent/session", new: true},
      {type: "habab_home/beta/activate"},
      {type: "habab_home/agent/session", new: true},
    ]);
  });

  await t.test("quota and network errors never activate", async () => {
    for (const error of [{code: "rate_limited"}, new TypeError("offline")]) {
      const calls = [];
      const hass = {callWS: async (request) => { calls.push(request); throw error; }};
      await assert.rejects(requestAssistantSession(hass, options), (actual) => actual === error);
      assert.equal(calls.length, 1);
      assert.equal(calls[0].type, "habab_home/agent/session");
    }
  });

  await t.test("enrollment failure does not issue a second session request", async () => {
    const calls = [];
    const enrollmentError = {code: "habab_beta_unavailable"};
    const hass = {callWS: async (request) => {
      calls.push(request);
      if (request.type === "habab_home/agent/session") throw {code: "provider_required"};
      throw enrollmentError;
    }};
    await assert.rejects(requestAssistantSession(hass, options), (actual) => actual === enrollmentError);
    assert.deepEqual(calls.map(({type}) => type), ["habab_home/agent/session", "habab_home/beta/activate"]);
  });

  await t.test("owner change before activation blocks enrollment", async () => {
    let ownerCurrent = true;
    const calls = [];
    const hass = {callWS: async (request) => {
      calls.push(request);
      if (request.type === "habab_home/agent/session") { ownerCurrent = false; throw {code: "provider_required"}; }
      return {status: "ready"};
    }};
    await assert.rejects(requestAssistantSession(hass, {isCurrent: () => ownerCurrent, onActivating() {}}), (error) => error.code === "provider_required");
    assert.deepEqual(calls.map(({type}) => type), ["habab_home/agent/session"]);
  });

  await t.test("owner change during activation blocks the retry", async () => {
    let ownerCurrent = true;
    const calls = [];
    const hass = {callWS: async (request) => {
      calls.push(request);
      if (request.type === "habab_home/agent/session") throw {code: "provider_required"};
      ownerCurrent = false;
      return {status: "ready"};
    }};
    await assert.rejects(requestAssistantSession(hass, {isCurrent: () => ownerCurrent, onActivating() {}}), (error) => error.code === "not_found");
    assert.deepEqual(calls.map(({type}) => type), ["habab_home/agent/session", "habab_home/beta/activate"]);
  });

  await t.test("owner change before session result rejects the result", async () => {
    let ownerCurrent = true;
    const calls = [];
    const hass = {callWS: async (request) => {
      calls.push(request);
      if (request.type === "habab_home/agent/session") {
        if (calls.filter((item) => item.type === request.type).length === 1) throw {code: "provider_required"};
        ownerCurrent = false;
        return {session_id: "stale-owner"};
      }
      return {status: "existing"};
    }};
    await assert.rejects(requestAssistantSession(hass, {isCurrent: () => ownerCurrent, onActivating() {}}), (error) => error.code === "not_found");
    assert.equal(calls.filter(({type}) => type === "habab_home/agent/session").length, 2);
  });

  await t.test("malformed activation response rejects without retry", async () => {
    const calls = [];
    const hass = {callWS: async (request) => {
      calls.push(request);
      if (request.type === "habab_home/agent/session") throw {code: "provider_required"};
      return {status: "pending"};
    }};
    await assert.rejects(requestAssistantSession(hass, options), (error) => error.code === "habab_beta_invalid_response");
    assert.deepEqual(calls.map(({type}) => type), ["habab_home/agent/session", "habab_home/beta/activate"]);
  });
});
