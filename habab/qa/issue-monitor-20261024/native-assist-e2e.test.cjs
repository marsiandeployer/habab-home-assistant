"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  parseArgs,
  selectConversationAgent,
  priorConversationOptions,
  restoreExposureCommand,
  hiddenTargetWasDenied,
  hasContinuousObserver,
  validateFixtureActionChain,
  validateAuthenticatedAdmin,
  boundedErrorClass,
  publicProjection,
} = require("./native-assist-e2e.cjs");

const required = [
  "--auth-file", "/private/auth.json",
  "--runtime-identity", "/private/runtime.json",
  "--expected-image", "sha256:image",
  "--expected-source", "source-sha",
  "--expected-runtime", "docker",
  "--expected-product-version", "0.3.5",
  "--expected-ha-version", "2026.10.2",
];

test("live mode requires an explicit execution window id", () => {
  assert.equal(parseArgs(["node", "runner", ...required]).runLive, false);
  assert.throws(() => parseArgs(["node", "runner", ...required, "--run-live"]), /execution_window_required/);
  const live = parseArgs(["node", "runner", ...required, "--run-live", "--execution-window-id", "window-20261024-01"]);
  assert.equal(live.runLive, true);
});

test("auth current-user projection validates identity without requiring nonexistent is_active", () => {
  const frontendUser = { id: "admin-1", is_admin: true };
  const currentUser = { id: "admin-1", name: "QA Admin", is_owner: true, is_admin: true,
    credentials: [], mfa_modules: [] };
  assert.deepEqual(validateAuthenticatedAdmin(frontendUser, currentUser, { connected: true }), { user_id: "admin-1" });
  assert.throws(() => validateAuthenticatedAdmin(frontendUser, null, { connected: true }), /authenticated_admin_identity_required/);
  assert.throws(() => validateAuthenticatedAdmin(frontendUser, { ...currentUser, is_admin: false }, { connected: true }), /authenticated_admin_identity_required/);
  assert.throws(() => validateAuthenticatedAdmin(frontendUser, { ...currentUser, id: "other-user" }, { connected: true }), /authenticated_admin_identity_required/);
  assert.throws(() => validateAuthenticatedAdmin(frontendUser, currentUser, { connected: false }), /authenticated_admin_identity_required/);
});

test("failure diagnostics expose only bounded stage and error class", () => {
  assert.equal(boundedErrorClass(new TypeError("token=never-log-this")), "TypeError");
  assert.equal(boundedErrorClass({ message: "secret" }), "Error");
  const projected = publicProjection({
    runtime: {}, verdict: "FAIL", failure_stage: "auth_context", failure_class: "Error",
    failure_code: "authenticated_admin_identity_required", exception: "raw token",
  });
  assert.equal(projected.failure_stage, "auth_context");
  assert.equal(projected.failure_class, "Error");
  assert.equal(JSON.stringify(projected).includes("raw token"), false);
});

test("agent selection requires one HA-selected provider and rejects missing or ambiguous mappings", () => {
  const entries = [
    { entry_id: "entry-a", provider: "deepseek", model: "model-a", state: "loaded", assistant_selected: false,
      entities: { conversation: "conversation.habab_a" } },
    { entry_id: "entry-b", provider: "gemini", model: "model-b", state: "loaded", assistant_selected: true,
      entities: { conversation: "conversation.habab_b" } },
  ];
  const agents = [{ id: "conversation.habab_a" }, { id: "conversation.habab_b" }];
  assert.deepEqual(selectConversationAgent(entries, agents), {
    entry_id: "entry-b", provider: "gemini", model: "model-b", entity_id: "conversation.habab_b",
  });
  assert.throws(() => selectConversationAgent(entries.map((entry) => ({ ...entry, assistant_selected: true })), agents), /selected_provider_missing_or_ambiguous/);
  assert.throws(() => selectConversationAgent(entries.map((entry) => ({ ...entry, assistant_selected: false })), agents), /selected_provider_missing_or_ambiguous/);
  assert.throws(() => selectConversationAgent(entries, [{ id: "conversation.other" }]), /selected_native_agent_missing_or_ambiguous/);
});

test("exposure snapshot preserves absent/null versus an existing conversation option object", () => {
  assert.equal(priorConversationOptions({ options: {} }), null);
  assert.equal(priorConversationOptions({ options: { conversation: null } }), null);
  const prior = { should_expose: false, custom: "retained" };
  assert.deepEqual(priorConversationOptions({ options: { conversation: prior } }), prior);
  assert.deepEqual(restoreExposureCommand("input_boolean.qa_fixture", prior), {
    type: "config/entity_registry/update", entity_id: "input_boolean.qa_fixture",
    options_domain: "conversation", options: prior,
  });
  assert.throws(() => restoreExposureCommand("light.other", null), /fixture_entity_invalid/);
});

test("hidden-target gate requires an exact native intent call and exposure denial", () => {
  const attempted = [{ role: "assistant", tool_calls: [{ id: "hidden-call-1", type: "function", function: {
    name: "intent__HassTurnOn", arguments: JSON.stringify({ name: "input_boolean.qa_fixture" }),
  } }] }, { role: "tool_result", tool_call_id: "hidden-call-1", tool_name: "intent__HassTurnOn", error: true, result: {
    error: true, data: { error_text: "No matching entities are exposed to the assistant (given name 'input_boolean.qa_fixture')" },
  } }];
  assert.equal(hiddenTargetWasDenied(attempted, "input_boolean.qa_fixture"), true);
  assert.equal(hiddenTargetWasDenied(attempted, "input_boolean.some_other"), false);
  assert.equal(hiddenTargetWasDenied([{ role: "assistant", content: "That entity is not exposed." }], "input_boolean.qa_fixture"), false);
  assert.equal(hiddenTargetWasDenied([{ role: "assistant", tool_calls: [{ id: "hidden-call-1", function: {
    name: "intent__HassTurnOn", arguments: JSON.stringify({ name: "input_boolean.qa_fixture" }),
  } }] }, { role: "tool_result", tool_call_id: "other-call", tool_name: "intent__HassTurnOn", error: true,
    result: { error: true, data: { error_text: "No matching entities are exposed to the assistant" } } }], "input_boolean.qa_fixture"), false);
  assert.equal(hiddenTargetWasDenied([{ role: "tool_result", tool_call_id: "hidden-call-1", tool_name: "intent__HassTurnOn", error: false,
    result: { data: { error_text: "No matching entities are exposed to the assistant" } } }], "input_boolean.qa_fixture"), false);
  assert.equal(hiddenTargetWasDenied([{ role: "assistant", tool_calls: [{ id: "not-the-fixture", function: {
    name: "intent__HassTurnOn", arguments: JSON.stringify({ name: "Bedroom light" }),
  } }] }, attempted[1]], "input_boolean.qa_fixture"), false);
});

test("observer continuity fails closed after disconnect or connection replacement", () => {
  const start = { connected: true, same_connection: true, connection_identity: 1, disconnected_count: 0 };
  assert.equal(hasContinuousObserver(start, { ...start }), true);
  assert.equal(hasContinuousObserver(start, { ...start, connected: false }), false);
  assert.equal(hasContinuousObserver(start, { ...start, connection_identity: 2 }), false);
  assert.equal(hasContinuousObserver(start, { ...start, disconnected_count: 1 }), false);
});

test("fixture action accepts only the exact Core wrapper-to-leaf service chain", () => {
  const leaf = { domain: "input_boolean", service: "turn_on", service_data: { entity_id: "input_boolean.fixture" }, user_id: "owner-1" };
  const wrapper = { domain: "homeassistant", service: "turn_on", target: { entity_id: ["input_boolean.fixture"] }, user_id: "owner-1" };
  assert.deepEqual(validateFixtureActionChain([leaf], "input_boolean.fixture", "owner-1"), {
    valid: true, event_count: 1, wrapper_count: 0,
  });
  assert.deepEqual(validateFixtureActionChain([wrapper, leaf], "input_boolean.fixture", "owner-1"), {
    valid: true, event_count: 2, wrapper_count: 1,
  });
  assert.equal(validateFixtureActionChain([], "input_boolean.fixture", "owner-1").valid, false);
  assert.equal(validateFixtureActionChain([wrapper, { ...leaf, user_id: "other-owner" }], "input_boolean.fixture", "owner-1").valid, false);
  assert.equal(validateFixtureActionChain([{ ...wrapper, target: { entity_id: ["input_boolean.fixture", "light.other"] } }, leaf], "input_boolean.fixture", "owner-1").valid, false);
  assert.equal(validateFixtureActionChain([leaf, wrapper], "input_boolean.fixture", "owner-1").valid, false);
  assert.equal(validateFixtureActionChain([wrapper, leaf, leaf], "input_boolean.fixture", "owner-1").valid, false);
  assert.equal(validateFixtureActionChain([wrapper, leaf, { ...leaf, domain: "light" }], "input_boolean.fixture", "owner-1").valid, false);
});

test("public projection omits fixture ids, raw replies, user ids, and chat logs", () => {
  const projected = publicProjection({
    checked_at: "2026-10-24T00:00:00.000Z", target_alias: "accepted-owner-test",
    runtime: { identity: { image: "sha256:image", source: "source-sha", runtime: "docker" }, status: "running" },
    verdict: "PASS_PENDING_MANUAL_REVIEW", selected_provider: { provider: "gemini", model: "flash" },
    cases: { hidden_denial: { tool_observed: true, action_event_count: 0, actor_context_match: true,
      readback: "off", acl_denial_observed: true, reply: "private exact assistant reply" } },
    cleanup: { fixture_created: true, fixture_deleted: true, fixture_id: "helper-secret-id" },
    private_fixture_id: "input_boolean.qa_fixture", user_id: "private-user-id", raw_chat_log: "private history",
  });
  const json = JSON.stringify(projected);
  for (const value of ["private exact assistant reply", "helper-secret-id", "input_boolean.qa_fixture", "private-user-id", "private history"]) {
    assert.equal(json.includes(value), false);
  }
});
