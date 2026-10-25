import assert from "node:assert/strict";
import test from "node:test";

const originalHTMLElement = Object.getOwnPropertyDescriptor(globalThis, "HTMLElement");
Object.defineProperty(globalThis, "HTMLElement", { configurable: true, value: class HTMLElementStub {} });
const diagnostics = await import("../src/habab_home/frontend/diagnostics-panel.js?diagnostics-test");
if (originalHTMLElement) Object.defineProperty(globalThis, "HTMLElement", originalHTMLElement);
else delete globalThis.HTMLElement;

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
