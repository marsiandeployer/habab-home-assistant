"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { hasAcceptedTurn, hasContinuousObserver, sameDeviceInventoryCount,
  isReadyToSubmit, shouldDeferFixtureCleanup, hasSameDocument,
  hasNoDocumentNavigation, hasCompletedSendCallbacks, observedEventsIfContinuous, serviceEventProjection,
  serviceEventTargetsOnlyEntity, writePublicReport } = require("./service-call-e2e.cjs");

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

const observerStart = { connection_identity: 4, disconnected_count: 0, same_connection: true,
  connected: true, owner_matches: true };
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
assert.equal(hasContinuousObserver(observerStart, { ...observerStart, owner_matches: false }), false,
  "reject observer whose authenticated owner differs");
assert.deepEqual(observedEventsIfContinuous(observerStart, { ...observerStart }, []), [],
  "continuous observer may prove zero events");
assert.equal(observedEventsIfContinuous(observerStart, { ...observerStart, connected: false }, []), null,
  "disconnected observer silence is unknown, not zero events");

const documentIdentity = {
  document_marker: "marker-a", main_frame_id: "frame-a", main_loader_id: "loader-a",
  pathname: "/home/overview", chat_instance_id: 1, same_host_as_baseline: true,
  owner_user_id: "owner-a", session_id: "session-a",
};
assert.equal(hasSameDocument(documentIdentity, { ...documentIdentity }), true,
  "accept stable document, route, owner, session, and chat instance");
assert.equal(hasNoDocumentNavigation([]), true, "accept no recorded navigation");
assert.equal(hasNoDocumentNavigation([{ kind: "chat_host_attached" }]), true,
  "accept DOM changes that keep the document");
for (const event of [
  { kind: "cdp_document_request" }, { kind: "cdp_frame_navigated", main: true },
  { kind: "puppeteer_frame_navigated", main: true }, { kind: "pagehide" },
  { kind: "beforeunload" }, { kind: "document_domcontentloaded" },
  { kind: "cdp_navigated_within_document" },
]) {
  assert.equal(hasNoDocumentNavigation([event]), false, `reject document transition ${event.kind}`);
}
for (const [field, value] of [
  ["document_marker", "marker-b"], ["main_frame_id", "frame-b"], ["main_loader_id", "loader-b"],
  ["pathname", "/login"], ["chat_instance_id", 2], ["same_host_as_baseline", false],
  ["owner_user_id", "owner-b"], ["session_id", "session-b"],
]) {
  assert.equal(hasSameDocument(documentIdentity, { ...documentIdentity, [field]: value }), false,
    `reject changed document identity field ${field}`);
}

const callbackEvents = ["send_started", "send_accepted", "send_completed"].map((kind) => ({
  kind, case_id: "attempt:turn_on", document_marker: documentIdentity.document_marker,
  chat_instance_id: documentIdentity.chat_instance_id,
  ...(kind === "send_accepted" ? { accepted: true } : {}),
  ...(kind === "send_completed" ? { completed: true, final_busy: false, error_class: null } : {}),
}));
assert.equal(hasCompletedSendCallbacks(callbackEvents, "attempt:turn_on", documentIdentity), true,
  "accept actual settled send callbacks from current document and chat");
assert.equal(hasCompletedSendCallbacks(callbackEvents.slice(0, 2), "attempt:turn_on", documentIdentity), false,
  "reject missing Promise settlement callback");
assert.equal(hasCompletedSendCallbacks(callbackEvents, "attempt:other", documentIdentity), false,
  "reject callbacks from another prompt");
assert.equal(hasCompletedSendCallbacks(callbackEvents.map((event) => ({ ...event,
  ...(event.kind === "send_accepted" ? { accepted: false } : {}) })),
"attempt:turn_on", documentIdentity), false, "reject send that was not accepted");
assert.equal(hasCompletedSendCallbacks(callbackEvents.map((event) => ({ ...event,
  ...(event.kind === "send_completed" ? { final_busy: true } : {}) })),
"attempt:turn_on", documentIdentity), false, "reject a settled callback that remains busy");
assert.equal(hasCompletedSendCallbacks(callbackEvents.map((event) => ({ ...event,
  ...(event.kind === "send_completed" ? { document_marker: "new-document" } : {}) })),
"attempt:turn_on", documentIdentity), false, "reject callback from a changed document");
assert.equal(hasCompletedSendCallbacks(callbackEvents.map((event) => ({ ...event,
  ...(event.kind === "send_completed" ? { error_class: "NetworkError" } : {}) })),
"attempt:turn_on", documentIdentity), false, "reject settled Promise failure");

const projectedCall = serviceEventProjection({ event_type: "call_service", context: { user_id: "owner-a", id: "ctx-a" },
  data: { domain: "input_boolean", service: "turn_on", service_data: { entity_id: "input_boolean.qa_fixture" },
    target: { entity_id: ["input_boolean.qa_fixture"] } } }, 0);
assert.equal(serviceEventTargetsOnlyEntity(projectedCall, "input_boolean.qa_fixture"), true,
  "accept call targeted only at the exact fixture");
assert.equal(serviceEventTargetsOnlyEntity({ ...projectedCall, device_ids: ["device-x"] },
  "input_boolean.qa_fixture"), false, "reject additional device targets");
assert.equal(serviceEventTargetsOnlyEntity({ ...projectedCall, entity_ids: ["input_boolean.other"] },
  "input_boolean.qa_fixture"), false, "reject a different entity target");

const reservationDir = fs.mkdtempSync(path.join(os.tmpdir(), "habab-attempt-reservation-"));
const reservationFile = path.join(reservationDir, "attempt.json");
try {
  writePublicReport(reservationFile, { owner: "first" }, true);
  const original = fs.readFileSync(reservationFile, "utf8");
  assert.throws(() => writePublicReport(reservationFile, { owner: "second" }, true),
    (error) => error.message === "attempt_output_exists", "reject concurrent attempt reservation");
  assert.equal(fs.readFileSync(reservationFile, "utf8"), original,
    "reservation collision must not truncate or overwrite the winner");
} finally {
  fs.rmSync(reservationDir, { recursive: true, force: true });
}

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
