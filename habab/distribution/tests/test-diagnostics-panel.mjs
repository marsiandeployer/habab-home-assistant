import assert from "node:assert/strict";
import test from "node:test";

const originalHTMLElement = Object.getOwnPropertyDescriptor(globalThis, "HTMLElement");
const originalCustomElements = Object.getOwnPropertyDescriptor(globalThis, "customElements");
const definitions = new Map();
Object.defineProperty(globalThis, "HTMLElement", { configurable: true, value: class HTMLElementStub {} });
Object.defineProperty(globalThis, "customElements", {
  configurable: true,
  value: {get: (name) => definitions.get(name), define: (name, value) => definitions.set(name, value)},
});
const diagnostics = await import("../src/habab_home/frontend/diagnostics-panel.js?diagnostics-test");
const DiagnosticsPanel = definitions.get("habab-diagnostics-panel");
if (originalHTMLElement) Object.defineProperty(globalThis, "HTMLElement", originalHTMLElement);
else delete globalThis.HTMLElement;
if (originalCustomElements) Object.defineProperty(globalThis, "customElements", originalCustomElements);
else delete globalThis.customElements;

test("diagnostics filters serialize local time as explicit UTC and reject invalid scopes", () => {
  const localFrom = "2026-10-24T12:30";
  const localTo = "2026-10-24T13:45";
  const filters = diagnostics.normalizeDiagnosticsFilters({
    from: localFrom, to: localTo, channel: "voice", status: "cancelled", session_id: "session_123",
  });
  assert.equal(filters.from, new Date(localFrom).toISOString());
  assert.equal(filters.to, new Date(localTo).toISOString());
  assert.equal(filters.channel, "voice");
  assert.equal(filters.status, "cancelled");
  assert.equal(filters.session_id, "session_123");
  const query = new URLSearchParams(diagnostics.diagnosticQueryString(filters));
  assert.equal(query.get("channel"), "voice");
  assert.equal(query.get("status"), "cancelled");
  assert.equal(query.get("limit"), "20");
  assert.throws(() => diagnostics.normalizeDiagnosticsFilters({ from: "not-a-date" }), /invalid_time/);
  assert.throws(() => diagnostics.normalizeDiagnosticsFilters({ from: localTo, to: localFrom }), /invalid_time_range/);
  assert.throws(() => diagnostics.normalizeDiagnosticsFilters({ session_id: "../other" }), /invalid_session_id/);
});

test("diagnostic labels separate assistant language from recorded tool events", () => {
  const words = {
    unavailableStage: "Unavailable", userMessage: "User", assistantText: "Assistant text",
    voiceTranscript: "Voice transcript", toolInvocation: "Tool call recorded",
    toolOutput: "Tool result recorded", stageError: "Runtime error recorded", unknown: "Unknown",
    telegram: "Telegram", voice: "Realtime voice", web: "Web chat",
  };
  assert.equal(diagnostics.diagnosticEventLabel({ kind: "assistant_text" }, words), "Assistant text");
  assert.equal(diagnostics.diagnosticEventLabel({ kind: "tool_invocation", confirmation: "runtime_call_recorded" }, words), "Tool call recorded");
  assert.equal(diagnostics.diagnosticEventLabel({ kind: "tool_output", confirmation: "runtime_result_recorded" }, words), "Tool result recorded");
  assert.equal(diagnostics.diagnosticEventLabel({ kind: "stage_unavailable", available: false }, words), "Unavailable");
  assert.equal(diagnostics.diagnosticEventLabel({ kind: "stage_error", stage: "model_request", status: "error" }, words), "Runtime error recorded");
  assert.equal(diagnostics.diagnosticEventLabel({ kind: "assistant_text", confirmation: "not_applicable" }, words), "Assistant text");
  assert.equal(diagnostics.diagnosticChannelLabel("web", ["web", "voice"], words), "Realtime voice");
  assert.equal(diagnostics.diagnosticChannelLabel("voice", ["voice"], words), "Realtime voice");
  assert.equal(diagnostics.diagnosticChannelLabel("telegram", ["telegram"], words), "Telegram");
});

test("shared runtime logs require admin confirmation and use the instance export route", () => {
  assert.equal(diagnostics.runtimeLogsExportRequest(false, true), undefined);
  assert.equal(diagnostics.runtimeLogsExportRequest(true, false), undefined);
  assert.equal(diagnostics.runtimeLogsExportRequest(true, "yes"), undefined);
  assert.deepEqual(diagnostics.runtimeLogsExportRequest(true, true), {
    endpoint: "/api/habab_home/diagnostics/runtime-logs/export",
    body: {},
  });
});

test("selected-conversation export includes shared logs only after explicit opt-in", async () => {
  assert.deepEqual(diagnostics.selectedSessionExportScope("session_123", "web"), {
    session_id: "session_123", channel: "web", include_runtime_logs: false,
  });
  assert.deepEqual(diagnostics.selectedSessionExportScope("session_123", "web", true), {
    session_id: "session_123", channel: "web", include_runtime_logs: true,
  });

  const panel = Object.create(DiagnosticsPanel.prototype);
  const nodes = new Map([
    ["#download-session", {disabled: false}],
    ["#download-period", {disabled: false}],
    ["#download-runtime-logs", {disabled: false}],
    ["#include-runtime-logs", {disabled: true, checked: false}],
    ["#load-more", {hidden: false, disabled: false}],
  ]);
  Object.assign(panel, {
    _hass: {user: {is_admin: true}},
    selectedSession: {session_id: "session_123"},
    filters: {channel: "all"},
    exporting: false,
    cursor: undefined,
    loading: false,
    q: (selector) => nodes.get(selector),
    canExportPeriod: () => false,
  });

  panel.updateControls();
  assert.equal(nodes.get("#include-runtime-logs").disabled, false);
  panel._hass.user.is_admin = false;
  panel.updateControls();
  assert.equal(nodes.get("#include-runtime-logs").disabled, true);
  panel._hass.user.is_admin = true;
  panel.selectedSession = undefined;
  panel.updateControls();
  assert.equal(nodes.get("#include-runtime-logs").disabled, true);

  panel.selectedSession = {session_id: "session_123", channel: "web"};
  const requests = [];
  panel.downloadArchive = async (scope) => requests.push(scope);
  await panel.downloadSelectedSession();
  assert.deepEqual(requests.at(-1), {
    session_id: "session_123", channel: "web", include_runtime_logs: false,
  });
  nodes.get("#include-runtime-logs").checked = true;
  await panel.downloadSelectedSession();
  assert.deepEqual(requests.at(-1), {
    session_id: "session_123", channel: "web", include_runtime_logs: true,
  });
  assert.equal(nodes.get("#include-runtime-logs").checked, false);
});

test("refreshing the session list cancels an in-flight detail request", async () => {
  const panel = Object.create(DiagnosticsPanel.prototype);
  let resolveDetail;
  let detailSignal;
  Object.assign(panel, {
    _hass: {
      user: {id: "owner", is_admin: true},
      fetchWithAuth: (url, options) => {
        if (url.includes("/diagnostics/sessions/")) {
          detailSignal = options.signal;
          return new Promise((resolve) => { resolveDetail = resolve; });
        }
        return Promise.resolve({ok: true, json: async () => ({
          sessions: [], next_cursor: null, sources: {},
        })});
      },
    },
    filters: {from: "", to: "", channel: "all", status: "all", session_id: ""},
    sessions: [{session_id: "synthetic-session", channel: "web"}],
    selectedSession: undefined,
    detail: undefined,
    sources: {},
    cursor: undefined,
    loading: false,
    loadingDetail: false,
    requestVersion: 0,
    detailVersion: 0,
    q: (selector) => selector === "#include-runtime-logs" ? {checked: false} : undefined,
    renderStatus() {},
    renderSessions() {},
    renderDetail() {},
    renderSources() {},
    updateControls() {},
  });

  const detailRequest = panel.openSession({session_id: "synthetic-session", channel: "web"});
  await Promise.resolve();
  assert.ok(detailSignal);
  assert.equal(detailSignal.aborted, false);

  await panel.loadSessions(false);
  assert.equal(detailSignal.aborted, true);
  assert.equal(panel.selectedSession, undefined);
  assert.equal(panel.loadingDetail, false);

  resolveDetail({ok: true, json: async () => ({session: {session_id: "synthetic-session"}, events: [], sources: {}})});
  await detailRequest;
  assert.equal(panel.detail, undefined);
});

test("feedback retries reuse one idempotency UUID until its payload changes", () => {
  const first = diagnostics.feedbackAttemptFor("session_123", "  The answer missed the room.  ");
  const retry = diagnostics.feedbackAttemptFor("session_123", "The answer missed the room.", first);
  assert.strictEqual(retry, first);
  assert.match(first.idempotency_key, /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  const edited = diagnostics.feedbackAttemptFor("session_123", "A different note", first);
  assert.notEqual(edited.idempotency_key, first.idempotency_key);
  assert.equal(edited.feedback, "A different note");
  const anotherSession = diagnostics.feedbackAttemptFor("session_456", first.feedback, first);
  assert.notEqual(anotherSession.idempotency_key, first.idempotency_key);
});

test("feedback POST confirms only a locally saved receipt and keeps HTTP failures visible", async () => {
  const attempt = diagnostics.feedbackAttemptFor("session_123", "The answer missed the room.");
  let call;
  const receiptId = "22222222-2222-4222-8222-222222222222";
  const receipt = await diagnostics.postConversationFeedback(async (url, options) => {
    call = {url, options};
    return {ok: true, json: async () => ({receipt_id: receiptId, state: "saved_locally", issue_state: "not_configured"})};
  }, attempt);
  assert.equal(call.url, "/api/habab_home/feedback");
  assert.equal(call.options.method, "POST");
  assert.equal(call.options.cache, "no-store");
  assert.deepEqual(JSON.parse(call.options.body), {...attempt, consent: true});
  assert.equal(receipt.receipt_id, receiptId);
  await assert.rejects(diagnostics.postConversationFeedback(async () => ({ok: false, status: 503}), attempt), (error) => {
    assert.equal(error.status, 503);
    return true;
  });
  await assert.rejects(diagnostics.postConversationFeedback(async () => ({ok: true, json: async () => ({receipt_id: receiptId, state: "queued", issue_state: "created"})}), attempt), /feedback_invalid_response/);
});

test("diagnostic rendering helpers mask credentials and sensitive structured fields", () => {
  const text = diagnostics.sanitizeDiagnosticText("Bearer abc.def OPENAI_API_KEY=hidden sk-12345678901234567890 /home/private/config.yaml");
  assert.doesNotMatch(text, /abc\.def|hidden|sk-12345678901234567890|\/home\/private/);
  assert.match(text, /Bearer \[redacted\]/);
  const safe = diagnostics.safeDiagnosticValue({
    api_key: "should-not-render", private_key: "also-secret", safe: "visible", nested: {Authorization: "also-secret"},
  });
  assert.equal(safe.api_key, "[redacted]");
  assert.equal(safe.private_key, "[redacted]");
  assert.equal(safe.safe, "visible");
  assert.equal(safe.nested.Authorization, "[redacted]");
});

test("diagnostic text redacts short prefixed environment secrets and OAuth query values", () => {
  const text = diagnostics.sanitizeDiagnosticText(
    "export HABAB_OPENAI_API_KEY=env-short VITE_GOOGLE_OAUTH_TOKEN=oauth-short https://example.test/cb?oauth_token=query-oauth&id_token=query-id&view=chat",
  );
  assert.doesNotMatch(text, /env-short|oauth-short|query-oauth|query-id/);
  assert.match(text, /\?oauth_token=\[redacted\]&id_token=\[redacted\]&view=chat/);
});

test("timestamps tolerate missing or malformed backend values", () => {
  assert.equal(diagnostics.formatDiagnosticTimestamp(undefined), "—");
  assert.equal(diagnostics.formatDiagnosticTimestamp("invalid"), "—");
  assert.notEqual(diagnostics.formatDiagnosticTimestamp("2026-10-24T12:00:00Z", "ru"), "—");
});

test("provider and processing-time summaries show only validated metadata", () => {
  assert.equal(diagnostics.formatDiagnosticProviderModels([
    {provider: "habab", model: "gemini-flash"},
    {provider: "habab", model: "gemini-flash"},
    {provider: "openai", model: "gpt-test"},
    {provider: "private_key=hidden", model: "model"},
  ]), "habab — gemini-flash; openai — gpt-test; [redacted] — model");
  assert.equal(diagnostics.formatDiagnosticProviderModels([]), "");
  assert.equal(diagnostics.formatDiagnosticDuration(840), "840 ms");
  assert.equal(diagnostics.formatDiagnosticDuration(2500), "2.5 s");
  assert.equal(diagnostics.formatDiagnosticDuration(65000), "1 min 5 s");
  assert.equal(diagnostics.formatDiagnosticDuration(-1), undefined);
  assert.equal(diagnostics.formatDiagnosticDuration("1000"), undefined);
});
