import assert from "node:assert/strict";
import test from "node:test";

// Only asynchronous action/scope logic is tested; no rendering assertions.
globalThis.HTMLElement = class {
  constructor() { this.isConnected = true; }
  attachShadow() { this.shadowRoot = {}; }
};
globalThis.customElements = {get: () => undefined, define: () => {}};
const {HababAutomationSetup} = await import("../src/habab_home/frontend/automation-setup.js");

function record(kind = "automation", phase = "draft") {
  return {draft_id: "d".repeat(32), config_hash: "a".repeat(64), kind, phase,
    native_id: "habab_qa", config: kind === "automation"
      ? {id: "habab_qa", initial_state: false} : {name: "QA", initial: false}};
}

function fixture(overrides = {}) {
  const card = new HababAutomationSetup();
  card.render = () => {};
  card._reconcile = () => {};
  const calls = [];
  const hass = {
    user: {id: "alice", is_admin: true}, language: "en",
    callWS: async (msg) => {
      calls.push(msg);
      if (msg.operation === "prepare") return record("automation", "saving");
      if (msg.operation === "status") return {drafts: [record("automation", "saved")]};
      return {};
    },
    callApi: async (...args) => { calls.push(args); }, ...overrides,
  };
  card.hass = hass;
  card.sessionId = "web-one";
  card.records = [record()];
  return {card, hass, calls};
}

test("explicit card action prepares exact scope/hash then uses native API and reconciles", async () => {
  const {card, calls} = fixture();
  await card.save(card.records[0]);
  assert.equal(calls[0].operation, "prepare");
  assert.equal(calls[0].session_id, "web-one");
  assert.equal(calls[0].config_hash, "a".repeat(64));
  assert.deepEqual(calls[1], ["POST", "config/automation/config/habab_qa", {id: "habab_qa", initial_state: false}]);
  assert.equal(calls[2].operation, "status");
  assert.equal(card.records[0].phase, "saved");
});

test("model draft/config load does not itself call native write", async () => {
  const {card, calls} = fixture();
  await card.refresh();
  assert.deepEqual(calls.map((call) => call.operation), ["status"]);
});

test("lost native POST response performs readback without retrying the write", async () => {
  let writes = 0;
  const {card, calls} = fixture({callApi: async () => { writes += 1; throw new Error("lost response"); }});
  await card.save(card.records[0]);
  await card.save(card.records[0]);
  assert.equal(writes, 1);
  assert.equal(calls.filter((call) => call.operation === "prepare").length, 1);
  assert.equal(card.records[0].phase, "saved");
});

test("new chat while preparation awaits prevents native write and stale callbacks", async () => {
  let resolvePrepare;
  let writes = 0;
  const {card} = fixture({
    callWS: async () => new Promise((resolve) => { resolvePrepare = resolve; }),
    callApi: async () => { writes += 1; },
  });
  const pending = card.save(card.records[0]);
  card.sessionId = "new-web";
  resolvePrepare(record("automation", "saving"));
  await pending;
  assert.equal(writes, 0);
  assert.deepEqual(card.records, []);
  assert.equal(card.busy, false);
});

test("new chat while status awaits discards old receipts", async () => {
  let resolveStatus;
  const {card} = fixture({callWS: async () => new Promise((resolve) => { resolveStatus = resolve; })});
  const pending = card.refresh();
  card.sessionId = "new-web";
  resolveStatus({drafts: [record("automation", "saved")]});
  await pending;
  assert.deepEqual(card.records, []);
});

test("owner change or unavailable current owner context blocks confirmation", async () => {
  const {card, calls, hass} = fixture();
  card.isCurrentContext = () => false;
  await card.save(card.records[0]);
  assert.deepEqual(calls, []);
  card.isCurrentContext = () => true;
  card.hass = {...hass, user: {id: "bob", is_admin: true}};
  assert.deepEqual(card.records, []);
});

test("helper creation uses native collection and initial false exactly once", async () => {
  const calls = [];
  const {card} = fixture({callWS: async (msg) => {
    calls.push(msg);
    if (msg.operation === "prepare") return record("input_boolean", "saving");
    if (msg.operation === "status") return {drafts: [record("input_boolean", "saving")]};
    return {id: "qa"};
  }});
  card.records = [record("input_boolean")];
  await card.save(card.records[0]);
  await card.save(card.records[0]);
  assert.deepEqual(calls.filter((call) => call.type === "input_boolean/create"), [{type: "input_boolean/create", name: "QA", initial: false}]);
  assert.equal(calls.find((call) => call.operation === "acknowledge").native_id, "qa");
});

test("mismatched approval hash or enabled config never reaches native POST", async () => {
  for (const approved of [{...record("automation", "saving"), config_hash: "b".repeat(64)},
    {...record("automation", "saving"), config: {id: "habab_qa", initial_state: true}}]) {
    let writes = 0;
    const {card} = fixture({
      callWS: async (msg) => msg.operation === "prepare" ? approved : {drafts: [record()]},
      callApi: async () => { writes += 1; },
    });
    await card.save(card.records[0]);
    assert.equal(writes, 0);
  }
});

test("native helper error remains uncertain without repeating create or acknowledging a made-up ID", async () => {
  const calls = [];
  const {card} = fixture({callWS: async (msg) => {
    calls.push(msg);
    if (msg.operation === "prepare") return record("input_boolean", "saving");
    if (msg.operation === "status") return {drafts: [record("input_boolean", "saving")]};
    if (msg.type === "input_boolean/create") throw new Error("native response lost");
    return {};
  }});
  card.records = [record("input_boolean")];
  await card.save(card.records[0]);
  await card.save(card.records[0]);
  assert.equal(calls.filter((call) => call.type === "input_boolean/create").length, 1);
  assert.equal(calls.filter((call) => call.operation === "acknowledge").length, 0);
  assert.equal(card.records[0].phase, "saving");
});

test("previous-chat recovery receipts cannot authorize saving", async () => {
  const {card, calls} = fixture();
  card.records = [{...record(), from_previous_chat: true}];
  await card.save(card.records[0]);
  assert.deepEqual(calls, []);
});

test("card dismissal survives refresh and stays scoped to its owner and chat", () => {
  const previousStorage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  const values = new Map();
  Object.defineProperty(globalThis, "localStorage", {configurable: true, value: {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  }});
  try {
    const {card, hass} = fixture();
    const stale = record();
    const other = {...record(), draft_id: "e".repeat(32)};
    card.records = [stale, other];
    card.dismissCard(stale);
    assert.deepEqual(card._visibleRecords().map(({draft_id}) => draft_id), [other.draft_id]);

    const refreshed = new HababAutomationSetup();
    refreshed.render = () => {};
    refreshed.hass = hass;
    refreshed.sessionId = "web-one";
    refreshed.records = [stale, other];
    assert.deepEqual(refreshed._visibleRecords().map(({draft_id}) => draft_id), [other.draft_id]);

    refreshed.sessionId = "new-web";
    refreshed.records = [stale, other];
    assert.deepEqual(refreshed._visibleRecords().map(({draft_id}) => draft_id), [stale.draft_id, other.draft_id]);

    refreshed.sessionId = "web-one";
    refreshed.records = [stale, other];
    assert.deepEqual(refreshed._visibleRecords().map(({draft_id}) => draft_id), [other.draft_id], "a hidden card does not reappear behind a restore action");
  } finally {
    if (previousStorage) Object.defineProperty(globalThis, "localStorage", previousStorage);
    else delete globalThis.localStorage;
  }
});
