"use strict";

const assert = require("node:assert/strict");
const { hasAcceptedTurn, hasContinuousObserver, sameDeviceInventoryCount,
  isReadyToSubmit, shouldDeferFixtureCleanup } = require("./service-call-e2e.cjs");

const prompt = "Turn on the disposable QA helper";
const cappedTail = Array.from({ length: 58 }, (_, index) => ({ role: index % 2 ? "assistant" : "user", text: `old ${index}` }));
cappedTail.push({ role: "user", text: prompt }, { role: "assistant", text: "The helper is on." });
const completedProbe = { expected_text: prompt, submitted_text: prompt, accepted: true,
  completed: true, final_busy: false, status_error: false };
const idleUi = { busy: false, status_error: false };

assert.equal(cappedTail.length, 60);
assert.equal(hasAcceptedTurn(cappedTail, prompt, completedProbe, idleUi), true, "accept completed pair at 60-item cap");
assert.equal(hasAcceptedTurn(cappedTail, prompt, { ...completedProbe, accepted: false }, idleUi), false,
  "reject identical preexisting pair when submit was skipped");
assert.equal(hasAcceptedTurn(cappedTail, prompt, { ...completedProbe, completed: false }, idleUi), false,
  "reject unfinished turn");
assert.equal(hasAcceptedTurn(cappedTail, prompt, { ...completedProbe, final_busy: true }, idleUi), false,
  "reject a turn still busy");
assert.equal(hasAcceptedTurn(cappedTail, prompt, { ...completedProbe, submitted_text: "other prompt" }, idleUi), false,
  "reject a probe for another prompt");
assert.equal(hasAcceptedTurn(cappedTail, prompt, { ...completedProbe, status_error: true }, idleUi), false,
  "reject a turn ending in UI error");
assert.equal(hasAcceptedTurn(cappedTail, prompt, completedProbe, { busy: true, status_error: false }), false,
  "reject current UI still busy");
assert.equal(hasAcceptedTurn(cappedTail, prompt, completedProbe, { busy: false, status_error: true }), false,
  "reject current UI error");
assert.equal(hasAcceptedTurn(cappedTail, "different prompt", completedProbe, idleUi), false,
  "reject unrelated user/assistant tail");
assert.equal(hasAcceptedTurn(cappedTail.slice(0, -1), prompt, completedProbe, idleUi), false,
  "reject missing assistant reply");

assert.equal(shouldDeferFixtureCleanup(true, true), true, "retain fixture while backend reports active");
assert.equal(shouldDeferFixtureCleanup(true, null), true, "retain fixture when backend state is unknown");
assert.equal(shouldDeferFixtureCleanup(true, false), false, "allow cleanup after backend idle confirmation");
assert.equal(shouldDeferFixtureCleanup(false, null), false, "no active turn guard when no turn may be running");

const observerStart = { connection_identity: 4, disconnected_count: 0, same_connection: true, connected: true };
assert.equal(hasContinuousObserver(observerStart, { ...observerStart }), true,
  "accept same connected observer without disconnects");
assert.equal(hasContinuousObserver(observerStart, { ...observerStart, disconnected_count: 1 }), false,
  "reject observer silence after disconnect");
assert.equal(hasContinuousObserver(observerStart, { ...observerStart, connection_identity: 5 }), false,
  "reject replacement connection");
assert.equal(hasContinuousObserver(observerStart, { ...observerStart, same_connection: false }), false,
  "reject chat replacing the observed connection");
assert.equal(hasContinuousObserver(observerStart, { ...observerStart, connected: false }), false,
  "reject currently disconnected observer");
assert.equal(hasContinuousObserver({ ...observerStart, disconnected_count: 1 },
  { ...observerStart, disconnected_count: 1 }), false, "reject observer that disconnected before case start");

assert.equal(sameDeviceInventoryCount(12, 12), true, "accept unchanged device registry count");
assert.equal(sameDeviceInventoryCount(12, 13), false, "reject device registry count change");
assert.equal(sameDeviceInventoryCount(null, null), false, "reject unavailable device registry count");

const readyToSubmit = {
  chat_present: true, chat_visible: true, chat_not_inert: true,
  owner_admin: true, owner_matches: true, hook_available: true,
  session_ready: true, busy: false, running: false, diagnostics_active: false,
  connection_connected: true, connection_same: true, connection_disconnected_count: 0,
  document_visible: true, dialog_open: true, textarea_visible: true,
  textarea_not_inert: true, textarea_enabled: true, textarea_empty: true,
  send_visible: true, send_not_inert: true, send_enabled: true,
};
assert.equal(isReadyToSubmit(readyToSubmit), true, "accept fully ready native chat");
for (const [field, value] of [
  ["chat_present", false], ["chat_visible", false], ["chat_not_inert", false],
  ["owner_matches", false], ["hook_available", false], ["diagnostics_active", null],
  ["connection_connected", false], ["connection_same", false], ["connection_disconnected_count", 1],
  ["document_visible", false], ["dialog_open", false], ["textarea_visible", false],
  ["textarea_not_inert", false], ["textarea_enabled", false], ["textarea_empty", false],
  ["send_visible", false], ["send_not_inert", false], ["send_enabled", false],
  ["busy", true], ["running", true],
]) {
  assert.equal(isReadyToSubmit({ ...readyToSubmit, [field]: value }), false, `reject submit when ${field} is unsafe`);
}

process.stdout.write("completion correlation, observer continuity, inventory, and submit readiness checks passed\n");
