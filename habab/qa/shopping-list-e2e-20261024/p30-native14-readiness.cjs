const fs = require("node:fs");
const puppeteer = require("/root/homeassistent/habab/node_modules/puppeteer-core");
const { withOwner } = require("/root/.cache/habab-android/owner-auth-qa.cjs");

const base = "http://127.0.0.1:18124"; // Parent-owned bridge; read-only use only.
const reportPath = `${__dirname}/p30-native14-readiness-result.json`;
const report = {
  classification: "READ_ONLY_READINESS_DIAGNOSTIC",
  target: {
    runtime_id: "7119388d53035d71",
    source_revision: "9d795359c22cf2844884caa5affc94209a8120ca",
    source_files_matched: 59,
    frontend_payload_files_matched: 14816,
    frontend_version_expected: "14816",
  },
  max_wait_ms: 30000,
  snapshots: [],
  chat_prompt_sent: false,
  mutations_attempted: false,
};

function save() {
  fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
}

async function snapshot(page, elapsedMs) {
  const result = await page.evaluate(async (elapsed) => {
    const hass = document.querySelector("home-assistant")?.hass;
    const chat = document.querySelector("habab-setup-chat");
    const statusText = chat?.q?.("#status")?.textContent?.trim() || "";
    const statusError = Boolean(chat?.q?.("#status")?.classList.contains("error"));
    const statusEntry = Object.entries(chat?.t || {}).find(([, value]) => value === statusText);
    let agent = { queried: false };
    if (hass && typeof chat?.sessionId === "string" && chat.sessionId) {
      try {
        const result = await hass.callWS({ type: "habab_home/agent/diagnostics", session_id: chat.sessionId });
        const error = result?.error;
        agent = {
          queried: true,
          active: typeof result?.active === "boolean" ? result.active : null,
          error_name: typeof error?.name === "string" ? error.name : null,
          error_http_status: Number.isInteger(error?.http_status) ? error.http_status : null,
          error_flags: error && typeof error === "object"
            ? Object.fromEntries(Object.entries(error).filter(([key, value]) => key.startsWith("mentions_") || key === "quota" || key === "invalid_model" || key === "missing_reasoning_content").map(([key, value]) => [key, Boolean(value)]))
            : {},
        };
      } catch (error) {
        agent = { queried: true, query_error_code: typeof error?.code === "string" ? error.code : "other" };
      }
    }
    return {
      elapsed_ms: elapsed,
      home_assistant_present: Boolean(hass),
      admin: Boolean(hass?.user?.is_admin),
      websocket_connected: Boolean(hass?.connection?.connected),
      chat_present: Boolean(chat),
      session_id_present: Boolean(chat?.sessionId),
      busy: typeof chat?.busy === "boolean" ? chat.busy : null,
      assistant_running: Boolean(chat?.running),
      setup_restarting: Boolean(chat?.setupRestarting),
      status_key: statusEntry?.[0] || (statusText ? "unmapped_status" : "empty"),
      status_is_error: statusError,
      agent,
    };
  }, elapsedMs);
  report.snapshots.push(result);
  save();
  return result;
}

withOwner(async ({ authData }) => {
  let browser;
  let context;
  try {
    browser = await puppeteer.connect({ browserURL: "http://127.0.0.1:9222", defaultViewport: null });
    context = await browser.createBrowserContext();
    const page = await context.newPage();
    await page.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
    await page.evaluateOnNewDocument((origin, data) => {
      if (location.origin === origin) {
        localStorage.setItem("hassTokens", JSON.stringify(data));
        localStorage.setItem("selectedLanguage", JSON.stringify("ru"));
      }
    }, base, authData);
    await page.goto(`${base}/`, { waitUntil: "domcontentloaded", timeout: 45000 });
    const started = Date.now();
    let elapsed = 0;
    let current;
    while (elapsed <= 30000) {
      current = await snapshot(page, elapsed);
      if (current.admin && current.websocket_connected && current.chat_present && current.session_id_present && current.busy === false) break;
      if (elapsed === 30000) break;
      await new Promise((resolve) => setTimeout(resolve, Math.min(5000, 30000 - elapsed)));
      elapsed = Date.now() - started;
      elapsed = Math.min(elapsed, 30000);
    }
    report.elapsed_ms = elapsed;
    report.readiness_reached = Boolean(current?.admin && current.websocket_connected && current.chat_present && current.session_id_present && current.busy === false);
    report.classification = report.readiness_reached ? "READINESS_REACHED_READ_ONLY" : "READINESS_NOT_REACHED_READ_ONLY";
    save();
    console.log(JSON.stringify({ classification: report.classification, elapsed_ms: report.elapsed_ms, snapshots: report.snapshots }));
  } catch (error) {
    report.classification = "READINESS_DIAGNOSTIC_FAILED_READ_ONLY";
    report.error = `${error.name}: ${String(error.message).slice(0, 120)}`;
    save();
    console.error(JSON.stringify({ classification: report.classification, error: report.error }));
    process.exitCode = 1;
  } finally {
    if (context) await context.close();
    if (browser) browser.disconnect();
  }
}).catch((error) => {
  report.classification = "READINESS_AUTH_OR_BRIDGE_FAILED_READ_ONLY";
  report.error = `${error.name}: ${String(error.message).slice(0, 120)}`;
  save();
  console.error(JSON.stringify({ classification: report.classification, error: report.error }));
  process.exitCode = 1;
});
