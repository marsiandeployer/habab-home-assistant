"use strict";

// Synthetic UI verification for the owner-only diagnostics panel. This uses
// only local fixture HTML and a client-side mock of the three diagnostics API
// routes; it never connects to Home Assistant or reads conversation history.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const puppeteer = require("../../node_modules/puppeteer-core");
const {withOwnedChromium} = require("/root/tools/chrome-daemon/owned-browser.cjs");

const ROOT = path.resolve(__dirname, "../..");
const PANEL_PATH = path.join(ROOT, "distribution/src/habab_home/frontend/diagnostics-panel.js");
const REPORT_PATH = path.join(__dirname, "diagnostics-panel-browser.json");
const PANEL_SOURCE = fs.readFileSync(PANEL_PATH, "utf8");
const PANEL_SHA256 = crypto.createHash("sha256").update(PANEL_SOURCE).digest("hex");
const VIEWPORTS = {desktop: {width: 1280, height: 800}, mobile: {width: 390, height: 844}};
const ownerASessions = [
  {session_id: "synthetic-web-session", title: "Completed fixture", channel: "web", modes: ["web"], created_at: "2026-10-24T12:00:00Z", updated_at: "2026-10-24T12:00:03Z", status: "completed", message_count: 3, provider_models: [{provider: "habab-test", model: "synthetic-model"}], processing_duration_ms: 1200, at_message_limit: false},
  {session_id: "synthetic-error-session", title: "Error fixture", channel: "web", modes: ["web"], created_at: "2026-10-24T12:10:00Z", updated_at: "2026-10-24T12:10:01Z", status: "error", message_count: 2, provider_models: [], processing_duration_ms: 300, error: {name: "ProviderError", http_status: 502, invalid_model: false}},
  {session_id: "synthetic-cancelled-session", title: "Cancelled fixture", channel: "web", modes: ["web"], created_at: "2026-10-24T12:20:00Z", updated_at: "2026-10-24T12:20:01Z", status: "cancelled", message_count: 1, provider_models: [], processing_duration_ms: 0},
  {session_id: "synthetic-voice-session", title: "Realtime fixture", channel: "web", modes: ["web", "voice"], created_at: "2026-10-24T12:30:00Z", updated_at: "2026-10-24T12:30:02Z", status: "completed", message_count: 2, provider_models: [{provider: "habab-test", model: "synthetic-voice-model"}], processing_duration_ms: 800},
];
const sources = {
  history: {available: true, reason: null, limit_messages: 60, retention: "available_while_owner_mapping_exists"},
  backend_ack: {available: false, reason: "not_recorded"},
  model_request: {available: false, reason: "not_recorded"},
  opencode_files: {available: false, reason: "not_exposed_by_runtime"},
  home_assistant_logs: {available: false, reason: "not_collected"},
  voice_transport: {available: false, reason: "not_recorded"},
  native_assist_history: {available: false, reason: "not_recorded_by_habab"},
  cancellation: {available: false, reason: "not_durably_recorded"},
};
const events = {
  "synthetic-web-session": [
    {id: "fixture-stage-missing", at: "2026-10-24T12:00:00Z", channel: "web", stage: "backend_accept", kind: "stage_unavailable", role: null, text: null, available: false, reason: "not_recorded"},
    {id: "fixture-user", at: "2026-10-24T12:00:00Z", channel: "web", stage: "client_send", kind: "user_message", role: "user", text: "Turn on the synthetic test light", available: true},
    {id: "fixture-assistant", at: "2026-10-24T12:00:01Z", channel: "web", stage: "model_text", kind: "assistant_text", role: "assistant", text: "The request was handled. <img src=x onerror=\"window.__xssRan=true\">synthetic", duration_ms: 1200, provider: "habab-test", model: "synthetic-model", confirmation: "not_applicable", available: true},
    {id: "fixture-tool-call", at: "2026-10-24T12:00:02Z", channel: "web", stage: "tool_call", kind: "tool_invocation", role: "assistant", tool: {name: "light.turn_on"}, input: {entity_id: "light.synthetic"}, correlation_id: "fixture-correlation", confirmation: "runtime_call_recorded", available: true},
    {id: "fixture-tool-result", at: "2026-10-24T12:00:03Z", channel: "web", stage: "tool_result", kind: "tool_output", role: "tool", output: {success: true, api_key: "SYNTHETIC_FIELD_SECRET", text: "OAUTH_TOKEN=SYNTHETIC_TEXT_SECRET"}, correlation_id: "fixture-correlation", confirmation: "runtime_result_recorded", status: "completed", available: true},
  ],
  "synthetic-error-session": [
    {id: "fixture-error", at: "2026-10-24T12:10:01Z", channel: "web", stage: "model_request", kind: "stage_error", role: "assistant", output: {name: "ProviderError", http_status: 502, quota: false}, status: "error", confirmation: "runtime_error_recorded", available: true},
  ],
  "synthetic-cancelled-session": [
    {id: "fixture-cancel", at: "2026-10-24T12:20:01Z", channel: "web", stage: "cancellation", kind: "stage_error", role: "assistant", output: {name: "MessageAbortedError", http_status: null}, status: "cancelled", confirmation: "runtime_error_recorded", available: true},
  ],
  "synthetic-voice-session": [
    {id: "fixture-voice-input", at: "2026-10-24T12:30:01Z", channel: "voice", stage: "model_text", kind: "voice_transcript", role: "user", text: "Synthetic voice transcript", available: true},
  ],
};

function waitForSelector(page, expression) {
  return page.waitForFunction(expression, {timeout: 5000});
}

async function main() {
  const report = {
    schema_version: 1,
    source_sha256: PANEL_SHA256,
    environment: {browser: "owned headless Chromium", fixture_origin: "loopback only", authentication: "synthetic admin with mocked fetchWithAuth", real_home_assistant: false, real_backend: false, real_history: false, model_calls: false, user_credentials: false, real_microphone: false},
    viewports: VIEWPORTS,
    checks: {},
    fixture_api_requests: [],
    browser_errors: {console_error_count: 0, page_error_count: 0, failed_request_count: 0, external_request_count: 0, external_request_origins: []},
    known_source_mismatch: null,
  };
  let currentStage = "starting_fixture";
  const server = http.createServer((request, response) => {
    if (request.url === "/" || request.url === "/favicon.ico") {
      if (request.url === "/favicon.ico") { response.writeHead(204); response.end(); return; }
      response.writeHead(200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"});
      response.end("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Diagnostics fixture</title></head><body><habab-diagnostics-panel id=\"panel\"></habab-diagnostics-panel><script type=\"module\">import './diagnostics-panel.js'; await customElements.whenDefined('habab-diagnostics-panel'); window.__panelReady = true;</script></body></html>");
      return;
    }
    if (request.url === "/diagnostics-panel.js") {
      response.writeHead(200, {"Content-Type": "text/javascript; charset=utf-8", "Cache-Control": "no-store"});
      response.end(PANEL_SOURCE);
      return;
    }
    response.writeHead(404); response.end();
  });

  await new Promise((resolve, reject) => server.listen(0, "127.0.0.1", (error) => error ? reject(error) : resolve()));
  const address = server.address();
  const fixtureOrigin = `http://127.0.0.1:${address.port}`;

  try {
    await withOwnedChromium(puppeteer, {taskTimeoutMs: 120000, defaultViewport: {width: 1280, height: 800}}, async ({page}) => {
      page.setDefaultTimeout(5000);
      await page.setRequestInterception(true);
      page.on("console", (message) => { if (message.type() === "error") report.browser_errors.console_error_count++; });
      page.on("pageerror", () => report.browser_errors.page_error_count++);
      page.on("requestfailed", () => report.browser_errors.failed_request_count++);
      page.on("request", (request) => {
        const requestUrl = new URL(request.url());
        if (requestUrl.origin !== fixtureOrigin && !["blob:", "data:", "about:"].includes(requestUrl.protocol)) {
          report.browser_errors.external_request_count++;
          report.browser_errors.external_request_origins.push(`${requestUrl.protocol}//${requestUrl.host}`);
          void request.abort();
        } else void request.continue();
      });
      currentStage = "open_local_fixture";
      await page.goto(fixtureOrigin, {waitUntil: "networkidle2"});
      await waitForSelector(page, () => window.__panelReady === true);
      currentStage = "assign_synthetic_admin";
      await page.evaluate(({ownerASessions, sources, events}) => {
        window.__fixtureApiRequests = [];
        window.__exportScopes = [];
        window.__downloadClick = null;
        window.__makeHass = (id, isAdmin = true) => ({
          user: {id, is_admin: isAdmin}, locale: {language: "en"},
          fetchWithAuth: async (path, options = {}) => {
            const url = new URL(path, location.origin);
            const method = options.method || "GET";
            window.__fixtureApiRequests.push({method, pathname: url.pathname});
            if (method === "GET" && url.pathname === "/api/habab_home/diagnostics/sessions") {
              let sessions = id === "synthetic-admin-a" ? [...ownerASessions] : [];
              const status = url.searchParams.get("status");
              const channel = url.searchParams.get("channel");
              const sessionId = url.searchParams.get("session_id");
              if (status && status !== "all") sessions = sessions.filter((session) => session.status === status);
              if (sessionId) sessions = sessions.filter((session) => session.session_id === sessionId);
              if (channel === "voice") sessions = sessions.filter((session) => session.modes?.includes("voice"));
              if (channel === "telegram" || channel === "web") sessions = sessions.filter((session) => session.channel === channel);
              return new Response(JSON.stringify({sessions, next_cursor: null, sources}), {status: 200, headers: {"Content-Type": "application/json"}});
            }
            const detailMatch = url.pathname.match(/^\/api\/habab_home\/diagnostics\/sessions\/(synthetic-[a-z-]+)$/);
            if (method === "GET" && detailMatch) {
              const session = ownerASessions.find((entry) => entry.session_id === detailMatch[1]);
              if (!session || id !== "synthetic-admin-a") return new Response("", {status: 404});
              return new Response(JSON.stringify({session, events: events[session.session_id] || [], sources}), {status: 200, headers: {"Content-Type": "application/json"}});
            }
            if (method === "POST" && url.pathname === "/api/habab_home/diagnostics/export") {
              window.__exportScopes.push(JSON.parse(options.body));
              return new Response(new Uint8Array([0x50, 0x4b, 0x03, 0x04]), {status: 200, headers: {"Content-Type": "application/zip", "Content-Disposition": "attachment; filename=\"habab-diagnostics.zip\""}});
            }
            return new Response("", {status: 404});
          },
        });
        HTMLAnchorElement.prototype.click = function() {
          window.__downloadClick = {download: this.download, hasBlobUrl: this.href.startsWith("blob:")};
        };
        document.querySelector("#panel").hass = window.__makeHass("synthetic-admin-a");
      }, {ownerASessions, sources, events});

      await waitForSelector(page, () => document.querySelector("#panel")?.shadowRoot?.querySelectorAll(".session").length === 4);
      currentStage = "desktop_layout";
      report.checks.desktop_two_column_layout = await page.evaluate(() => {
        return getComputedStyle(document.querySelector("#panel").shadowRoot.querySelector(".workspace")).gridTemplateColumns.trim().split(/\s+/).length === 2;
      });

      await page.evaluate(() => {
        const panel = document.querySelector("#panel");
        panel.shadowRoot.querySelector("#status-filter").value = "cancelled";
        panel.shadowRoot.querySelector("#filters").dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}));
      });
      await waitForSelector(page, () => {
        const list = document.querySelector("#panel")?.shadowRoot?.querySelectorAll(".session");
        return list?.length === 1 && list[0].textContent.includes("Cancelled fixture");
      });
      currentStage = "cancelled_filter";
      report.checks.cancelled_status_filter = true;

      await page.evaluate(() => {
        const panel = document.querySelector("#panel");
        panel.shadowRoot.querySelector("#status-filter").value = "all";
        panel.shadowRoot.querySelector("#filters").dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}));
      });
      await waitForSelector(page, () => document.querySelector("#panel")?.shadowRoot?.querySelectorAll(".session").length === 4);
      currentStage = "completed_session_detail";
      await page.evaluate(() => document.querySelector("#panel").shadowRoot.querySelectorAll(".session")[0].click());
      await waitForSelector(page, () => document.querySelector("#panel")?.shadowRoot?.querySelector("#detail")?.textContent.includes("Tool result recorded"));
      currentStage = "detail_security_evidence";
      const detailEvidence = await page.evaluate(() => {
        const root = document.querySelector("#panel").shadowRoot;
        const text = root.querySelector("#detail").textContent;
        return {
          hasUserText: text.includes("Turn on the synthetic test light"),
          hasAssistantText: text.includes("The request was handled."),
          hasToolCallAndResult: text.includes("Tool invocation recorded") && text.includes("Tool result recorded"),
          hasEvidenceCaveat: text.includes("not an independent Home Assistant state readback"),
          secretMasked: !text.includes("SYNTHETIC_FIELD_SECRET") && !text.includes("SYNTHETIC_TEXT_SECRET"),
          htmlRenderedAsText: root.querySelector("#detail img") === null && text.includes("<img") && window.__xssRan !== true,
          nativeAssistUnavailable: root.querySelector("#native-assist-note")?.textContent.includes("not recorded by Habab"),
        };
      });
      Object.assign(report.checks, {
        user_assistant_tool_result_timeline: detailEvidence.hasUserText && detailEvidence.hasAssistantText && detailEvidence.hasToolCallAndResult,
        evidence_caveat_visible: detailEvidence.hasEvidenceCaveat,
        synthetic_secrets_masked: detailEvidence.secretMasked,
        dynamic_html_rendered_as_text: detailEvidence.htmlRenderedAsText,
        native_assist_unavailable_disclosed: detailEvidence.nativeAssistUnavailable,
      });

      await page.evaluate(() => {
        const panel = document.querySelector("#panel");
        [...panel.shadowRoot.querySelectorAll(".session")].find((button) => button.textContent.includes("Error fixture")).click();
      });
      await waitForSelector(page, () => document.querySelector("#panel")?.shadowRoot?.querySelector("#detail")?.textContent.includes("Runtime error recorded"));
      currentStage = "error_session_detail";
      report.checks.runtime_error_detail = await page.evaluate(() => document.querySelector("#panel").shadowRoot.querySelector("#detail").textContent.includes("Status: Error"));

      await page.evaluate(() => {
        const panel = document.querySelector("#panel");
        [...panel.shadowRoot.querySelectorAll(".session")].find((button) => button.textContent.includes("Cancelled fixture")).click();
      });
      await waitForSelector(page, () => document.querySelector("#panel")?.shadowRoot?.querySelector("#detail")?.textContent.includes("Status: Cancelled"));
      currentStage = "cancellation_session_detail";
      report.checks.cancellation_stage_detail = await page.evaluate(() => {
        const text = document.querySelector("#panel").shadowRoot.querySelector("#detail").textContent;
        return text.includes("Runtime error recorded") && text.includes("MessageAbortedError");
      });

      await page.evaluate(() => {
        const panel = document.querySelector("#panel");
        [...panel.shadowRoot.querySelectorAll(".session")].find((button) => button.textContent.includes("Completed fixture")).click();
      });
      await waitForSelector(page, () => document.querySelector("#panel")?.shadowRoot?.querySelector("#detail")?.textContent.includes("Tool result recorded"));
      currentStage = "selected_export";

      await page.evaluate(() => document.querySelector("#panel").shadowRoot.querySelector("#download-session").click());
      await waitForSelector(page, () => window.__downloadClick?.hasBlobUrl === true);
      report.checks.selected_zip_download_initiated = await page.evaluate(() => window.__downloadClick.download === "habab-diagnostics.zip");

      await page.evaluate(() => {
        const panel = document.querySelector("#panel");
        panel.shadowRoot.querySelector("#from").value = "2026-10-22T00:00";
        panel.shadowRoot.querySelector("#to").value = "2026-10-27T00:00";
        panel.shadowRoot.querySelector("#session-id").value = "synthetic-cancelled-session";
        panel.shadowRoot.querySelector("#status-filter").value = "cancelled";
        panel.shadowRoot.querySelector("#filters").dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}));
      });
      await waitForSelector(page, () => document.querySelector("#panel")?.shadowRoot?.querySelectorAll(".session").length === 1);
      await page.evaluate(() => document.querySelector("#panel").shadowRoot.querySelector("#download-period").click());
      await waitForSelector(page, () => window.__exportScopes.length === 2);
      currentStage = "period_export_scope";
      const scopes = await page.evaluate(() => window.__exportScopes);
      const periodScope = scopes[1];
      report.checks.period_zip_initiated = true;
      report.checks.period_export_scope_is_time_and_channel_only = Boolean(periodScope.from && periodScope.to && !("session_id" in periodScope) && !("status" in periodScope));

      await page.evaluate(() => {
        document.querySelector("#panel").hass = window.__makeHass("synthetic-admin-b");
      });
      await waitForSelector(page, () => {
        const panel = document.querySelector("#panel");
        return panel?.shadowRoot?.querySelector("#session-list .empty") && panel.shadowRoot.querySelector("#detail")?.textContent.includes("Select a conversation");
      });
      currentStage = "admin_owner_switch";
      report.checks.admin_account_change_clears_previous_owner = await page.evaluate(() => {
        const text = document.querySelector("#panel").shadowRoot.textContent;
        return !text.includes("Completed fixture") && !text.includes("SYNTHETIC_FIELD_SECRET");
      });

      await page.evaluate(() => { document.querySelector("#panel").hass = window.__makeHass("synthetic-guest", false); });
      await waitForSelector(page, () => document.querySelector("#panel")?.shadowRoot?.querySelector("#status")?.textContent.includes("administrators only"));
      currentStage = "non_admin_acl";
      report.checks.non_admin_clears_and_denies = await page.evaluate(() => {
        const root = document.querySelector("#panel").shadowRoot;
        return root.querySelector("#status").textContent.includes("administrators only") && !root.textContent.includes("Completed fixture");
      });

      await page.setViewport({width: 390, height: 844});
      currentStage = "mobile_layout";
      await page.evaluate(() => { document.querySelector("#panel").hass = window.__makeHass("synthetic-admin-a"); });
      await page.evaluate(() => {
        const panel = document.querySelector("#panel");
        panel.shadowRoot.querySelector("#status-filter").value = "all";
        panel.shadowRoot.querySelector("#session-id").value = "";
        panel.shadowRoot.querySelector("#from").value = "";
        panel.shadowRoot.querySelector("#to").value = "";
        panel.shadowRoot.querySelector("#filters").dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}));
      });
      await waitForSelector(page, () => document.querySelector("#panel")?.shadowRoot?.querySelectorAll(".session").length === 4);
      report.checks.mobile_single_column_layout_and_no_horizontal_overflow = await page.evaluate(() => {
        const panel = document.querySelector("#panel");
        const root = panel.shadowRoot;
        return getComputedStyle(root.querySelector(".workspace")).gridTemplateColumns.trim().split(/\s+/).length === 1 && document.documentElement.scrollWidth <= innerWidth;
      });

      await page.evaluate(() => {
        const panel = document.querySelector("#panel");
        panel.shadowRoot.querySelector("#channel").value = "voice";
        panel.shadowRoot.querySelector("#filters").dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}));
      });
      await waitForSelector(page, () => document.querySelector("#panel")?.shadowRoot?.querySelectorAll(".session").length === 1);
      currentStage = "realtime_voice_channel_label";
      const voiceLabel = await page.evaluate(() => document.querySelector("#panel").shadowRoot.querySelector(".session-meta").textContent);
      report.checks.realtime_voice_label_correct = voiceLabel.includes("Realtime voice");
      report.known_source_mismatch = report.checks.realtime_voice_label_correct
        ? null : {expected: "Realtime voice", observed: voiceLabel, source_defect: true};

      currentStage = "request_summary";
      report.fixture_api_requests = await page.evaluate(() => window.__fixtureApiRequests.reduce((counts, entry) => {
        counts[`${entry.method} ${entry.pathname.replace(/^\/api\/habab_home\/diagnostics\/.*/, "/api/habab_home/diagnostics/{resource}")}`] =
          (counts[`${entry.method} ${entry.pathname.replace(/^\/api\/habab_home\/diagnostics\/.*/, "/api/habab_home/diagnostics/{resource}")}`] || 0) + 1;
        return counts;
      }, {}));
      if (Object.values(report.checks).some((passed) => passed !== true)) throw new Error("synthetic_browser_check_failed");
    });
    report.status = "passed";
  } catch (error) {
    report.status = "failed";
    report.failure = error?.message === "synthetic_browser_check_failed" ? "one_or_more_assertions_failed" : "browser_harness_error";
    report.failure_stage = currentStage;
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }

  fs.writeFileSync(REPORT_PATH, `${JSON.stringify(report, null, 2)}\n`, {flag: "wx", mode: 0o644});
  process.stdout.write(`${JSON.stringify({status: report.status, checks: report.checks, known_source_mismatch: report.known_source_mismatch, browser_errors: report.browser_errors, panel_sha256: report.source_sha256})}\n`);
  if (report.status === "failed") process.exitCode = 1;
}

main().catch((error) => {
  process.stderr.write(`${error?.message || "browser_harness_error"}\n`);
  process.exitCode = 1;
});
