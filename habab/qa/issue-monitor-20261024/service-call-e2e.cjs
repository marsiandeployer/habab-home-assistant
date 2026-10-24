"use strict";

// Real web-chat E2E for one disposable input_boolean. Never uses setup_actions
// as a test surrogate: only the browser chat sends the three user prompts.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const puppeteer = require("../../node_modules/puppeteer-core");
const { withOwnedChromium } = require("/root/tools/chrome-daemon/owned-browser.cjs");

const OUTPUT = path.join(__dirname, "service-call-e2e.json");
const FIXTURE_NAME = "Habab QA service-call fixture 20261024";
const FIXTURE_ID_PREFIX = "habab_qa_service_call_20261024";
const PRIVATE_TRACE_NAME = "service-call-e2e-private.json";
const ALLOWED_ORIGIN = "https://habab-demo.wpmix.net";

function args(argv) {
  const parsed = { runLive: false };
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    if (key === "--run-live") {
      parsed.runLive = true;
      continue;
    }
    if (!key.startsWith("--") || i + 1 >= argv.length) {
      throw new Error("invalid_arguments");
    }
    parsed[key.slice(2)] = argv[++i];
  }
  for (const key of ["auth-file", "runtime-identity", "expected-image", "expected-source", "expected-runtime", "expected-product-version", "expected-ha-version"]) {
    if (typeof parsed[key] !== "string" || !parsed[key]) throw new Error("missing_required_argument");
  }
  if (parsed.runLive && !parsed["execution-window-id"]) throw new Error("execution_window_required");
  return parsed;
}

function readJson(file, code) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    throw new Error(code);
  }
}

function privateWrite(file, value) {
  fs.writeFileSync(file, JSON.stringify(value, null, 2) + "\n", { mode: 0o600 });
  fs.chmodSync(file, 0o600);
}

function runtimeInputs(options) {
  const auth = readJson(options["auth-file"], "auth_file_unreadable");
  if (typeof auth.access_token !== "string" || !auth.access_token) throw new Error("access_token_missing");
  if (auth.base_url) {
    let configured;
    try {
      configured = new URL(auth.base_url);
    } catch {
      throw new Error("auth_target_invalid");
    }
    if (configured.origin !== ALLOWED_ORIGIN || !["", "/", "/owner-test"].includes(configured.pathname)) {
      throw new Error("target_not_allowed");
    }
  }
  const identity = readJson(options["runtime-identity"], "runtime_identity_unreadable");
  const image = identity.image || identity.image_sha256;
  const runtimeName = identity.runtime;
  if (image !== options["expected-image"] || identity.source !== options["expected-source"]
      || runtimeName !== options["expected-runtime"]) {
    throw new Error("runtime_identity_mismatch");
  }
  return {
    auth,
    base: `${ALLOWED_ORIGIN}/owner-test`,
    origin: ALLOWED_ORIGIN,
    identity: { image, runtime: runtimeName, source: identity.source },
  };
}

async function getJson(url, runtime, code) {
  let response;
  try {
    response = await fetch(url, {
      headers: { Authorization: `Bearer ${runtime.auth.access_token}`, Cookie: runtime.cookieHeader || "" },
      redirect: "manual",
    });
  } catch {
    throw new Error(code);
  }
  if (!response.ok) throw new Error(`${code}_${response.status}`);
  try {
    return await response.json();
  } catch {
    throw new Error(code);
  }
}

async function readState(runtime, entityId) {
  let response;
  try {
    response = await fetch(`${runtime.origin}/api/states/${encodeURIComponent(entityId)}`, {
      headers: { Authorization: `Bearer ${runtime.auth.access_token}`, Cookie: runtime.cookieHeader || "" },
      redirect: "manual",
    });
  } catch {
    throw new Error("state_read_failed");
  }
  if (response.status === 404) return null;
  if (!response.ok) throw new Error("state_read_failed");
  try {
    const state = await response.json();
    return { state: state.state, friendly_name: state.attributes?.friendly_name };
  } catch {
    throw new Error("state_read_failed");
  }
}

async function bootstrapOwnerSession(runtime) {
  const cookieJar = new Map();
  let url = runtime.base;
  for (let hop = 0; hop < 4; hop += 1) {
    let response;
    try {
      const headers = { Authorization: `Bearer ${runtime.auth.access_token}` };
      if (cookieJar.size) headers.Cookie = [...cookieJar].map(([name, value]) => `${name}=${value}`).join("; ");
      response = await fetch(url, { headers, redirect: "manual" });
    } catch {
      throw new Error("owner_session_bootstrap_failed");
    }
    const responseUrl = new URL(response.url);
    if (responseUrl.origin !== runtime.origin || responseUrl.protocol !== "https:") {
      throw new Error("owner_session_target_rejected");
    }
    for (const header of response.headers.getSetCookie()) {
      const pair = header.split(";", 1)[0];
      const separator = pair.indexOf("=");
      if (separator > 0) cookieJar.set(pair.slice(0, separator), pair.slice(separator + 1));
    }
    if (response.status >= 300 && response.status < 400) {
      const location = response.headers.get("location");
      if (!location || hop === 3) throw new Error("owner_session_redirect_rejected");
      const next = new URL(location, url);
      if (next.origin !== runtime.origin || next.protocol !== "https:" || next.username || next.password) {
        throw new Error("owner_session_redirect_rejected");
      }
      url = next.href;
      continue;
    }
    if (response.status < 200 || response.status >= 300) {
      throw new Error(`owner_session_bootstrap_failed_${response.status}`);
    }
    runtime.cookieHeader = [...cookieJar].map(([name, value]) => `${name}=${value}`).join("; ");
    return;
  }
  throw new Error("owner_session_redirect_rejected");
}

async function preflight(runtime, options) {
  await bootstrapOwnerSession(runtime);
  const status = await getJson(`${runtime.origin}/api/habab_home/status`, runtime, "status_read_failed");
  if (status.product !== "Habab Home"
      || status.version !== options["expected-product-version"]
      || status.home_assistant_version !== options["expected-ha-version"]
      || status.status !== "running") {
    throw new Error("runtime_status_mismatch");
  }
  return {
    identity: runtime.identity,
    product_version: status.version,
    home_assistant_version: status.home_assistant_version,
    status: status.status,
  };
}

async function main() {
  const options = args(process.argv);
  const runtime = runtimeInputs(options);
  const status = await preflight(runtime, options);
  const report = {
    checked_at: new Date().toISOString(),
    target_alias: "accepted-owner-test",
    runtime: status,
    mode: options.runLive ? "chat_e2e" : "read_only_preflight",
    verdict: options.runLive ? "NOT_RUN" : "PREFLIGHT_PASS",
    cases: {},
    cleanup: { fixture_created: false, fixture_deleted: false },
    limitations: [
      "A browser action trace and independent Home Assistant state readback are required for E2E acceptance.",
      "Physical device behavior is outside this nonphysical fixture test.",
    ],
  };
  if (!options.runLive) {
    process.stdout.write(JSON.stringify({ verdict: report.verdict, runtime: report.runtime }) + "\n");
    return;
  }

  const privateTracePath = path.join(path.dirname(path.resolve(options["auth-file"])), PRIVATE_TRACE_NAME);
  if (fs.existsSync(privateTracePath) || fs.existsSync(OUTPUT)) throw new Error("prior_run_artifact_exists");
  const privateTrace = {
    runtime: runtime.identity,
    helper_name: FIXTURE_NAME,
    prompts: [],
    replies: [],
    call_service_events: [],
    helper_id: null,
    entity_id: null,
  };
  let unsubscribeInstalled = false;
  let fixtureCreateAttempted = false;
  let fixtureCreated = false;
  let helperId;
  let entityId;
  let priorHelperIds = new Set();
  let chatTurnMayBeActive = false;
  let setupError;
  let safeToDeleteFixture = true;
  const persistPrivate = () => privateWrite(privateTracePath, privateTrace);
  const savePublic = () => fs.writeFileSync(OUTPUT, JSON.stringify(report, null, 2) + "\n", { mode: 0o644 });

  async function cleanupWhileBrowserOpen(page) {
    if (page && fixtureCreateAttempted && !fixtureCreated) {
      try {
        const newlyCreated = await page.evaluate((priorIds) => document.querySelector("habab-setup-chat").hass
          .callWS({ type: "input_boolean/list" }).then((helpers) => helpers.filter((item) =>
            item.name === "Habab QA service-call fixture 20261024" && !priorIds.includes(item.id))),
        [...priorHelperIds]);
        if (newlyCreated.length === 1 && typeof newlyCreated[0].id === "string") {
          helperId = newlyCreated[0].id;
          fixtureCreated = true;
          report.cleanup.fixture_created = true;
          privateTrace.helper_id = helperId;
          persistPrivate();
        } else if (newlyCreated.length > 1) {
          report.cleanup.fixture_deleted = false;
          report.verdict = "FAIL_CLEANUP";
          report.failure_code = "fixture_recovery_ambiguous";
        }
      } catch {
        report.cleanup.fixture_deleted = false;
        report.verdict = "FAIL_CLEANUP";
        report.failure_code = "fixture_recovery_failed";
      }
    }
    if (page && chatTurnMayBeActive) {
      try {
        const active = await page.evaluate(() => {
          const chat = document.querySelector("habab-setup-chat");
          if (chat?.busy) chat.shadowRoot.querySelector(".stop")?.click();
          return !!chat?.busy;
        });
        if (active) {
          await page.waitForFunction(() => !document.querySelector("habab-setup-chat")?.busy,
            { timeout: 20000 });
        }
        chatTurnMayBeActive = false;
      } catch {
        report.active_turn_stopped_before_cleanup = false;
        safeToDeleteFixture = false;
      }
    }
    if (page && unsubscribeInstalled) {
      try {
        await page.evaluate(async () => {
          if (window.__hababQaServiceUnsubscribe) await window.__hababQaServiceUnsubscribe();
        });
      } catch {
        report.service_event_subscription_closed = false;
      }
    }
    if (page && fixtureCreated && helperId) {
      if (!safeToDeleteFixture) {
        report.cleanup.fixture_deleted = false;
        report.verdict = "FAIL_CLEANUP";
        privateTrace.fixture_deleted = false;
        privateTrace.cleanup_deferred_active_turn = true;
        persistPrivate();
      } else {
        try {
          await page.evaluate((id) => document.querySelector("habab-setup-chat").hass.callWS({
            type: "input_boolean/delete", input_boolean_id: id,
          }), helperId);
          const remaining = await pageHelperExists(page, helperId);
          const deletedState = entityId ? await readState(runtime, entityId) : null;
          report.cleanup.fixture_deleted = !remaining && deletedState === null;
          if (!report.cleanup.fixture_deleted) report.verdict = "FAIL_CLEANUP";
          else if (report.verdict === "ACTIONS_PASS_CLEANUP_PENDING") report.verdict = "PASS";
          privateTrace.fixture_deleted = report.cleanup.fixture_deleted;
          persistPrivate();
        } catch {
          report.cleanup.fixture_deleted = false;
          report.verdict = "FAIL_CLEANUP";
          privateTrace.fixture_deleted = false;
          persistPrivate();
        }
      }
    }
    if (page) {
      try {
        await page.evaluate(() => localStorage.removeItem("hassTokens"));
      } catch {
        // The owned Chromium profile is removed by withOwnedChromium.
      }
    }
    savePublic();
  }

  try {
    await withOwnedChromium(puppeteer, {
      taskTimeoutMs: 900000,
      args: ["--disable-dev-shm-usage"],
      defaultViewport: { width: 1280, height: 800 },
    }, async ({ page }) => {
      try {
      await page.evaluateOnNewDocument((auth) => {
        localStorage.setItem("hassTokens", JSON.stringify({
          ...auth,
          hassUrl: "https://habab-demo.wpmix.net",
          clientId: auth.client_id,
          expires: Date.now() + (auth.expires_in || 3600) * 1000,
        }));
        localStorage.setItem("selectedLanguage", JSON.stringify("ru"));
      }, runtime.auth);
      await page.goto(runtime.base, { waitUntil: "networkidle2", timeout: 60000 });
      await page.waitForFunction(() => {
        const chat = document.querySelector("habab-setup-chat");
        return !!chat?.hass?.user?.is_admin && !!chat.hass.callWS && !!window.hababAssistant?.available;
      }, { timeout: 60000 });
      await page.waitForFunction(() => {
        const chat = document.querySelector("habab-setup-chat");
        return !!chat?.sessionId && !chat.busy;
      }, { timeout: 90000 });
      const ready = await page.evaluate(() => {
        const chat = document.querySelector("habab-setup-chat");
        if (!chat.shadowRoot.querySelector("dialog").open) chat.shadowRoot.querySelector(".launcher")?.click();
        return {
          dialog_open: chat.shadowRoot.querySelector("dialog").open,
          draft_empty: !chat.shadowRoot.querySelector("textarea").value.trim(),
          history_count: chat.messages.length,
        };
      });
      assert.equal(ready.dialog_open, true, "assistant_dialog_not_open");
      assert.equal(ready.draft_empty, true, "existing_draft_present");
      report.preexisting_history_count = ready.history_count;

      const helpersBefore = await page.evaluate(() => document.querySelector("habab-setup-chat").hass.callWS({ type: "input_boolean/list" }));
      assert.ok(Array.isArray(helpersBefore), "input_boolean_list_unavailable");
      assert.equal(helpersBefore.some((item) => item.name === FIXTURE_NAME), false, "fixture_already_exists");
      priorHelperIds = new Set(helpersBefore.map((item) => item.id).filter((id) => typeof id === "string"));

      fixtureCreateAttempted = true;
      const created = await page.evaluate((name) => {
        const hass = document.querySelector("habab-setup-chat").hass;
        return hass.callWS({ type: "input_boolean/create", name, icon: "mdi:toggle-switch", initial: false });
      }, FIXTURE_NAME);
      let createdId = created?.id;
      if (typeof createdId !== "string" || !createdId) {
        const afterCreate = await page.evaluate(() => document.querySelector("habab-setup-chat").hass.callWS({ type: "input_boolean/list" }));
        const matches = afterCreate.filter((item) => item.name === FIXTURE_NAME && !priorHelperIds.has(item.id));
        if (matches.length === 1 && typeof matches[0].id === "string") createdId = matches[0].id;
      }
      if (typeof createdId === "string" && createdId) {
        fixtureCreated = true;
        helperId = createdId;
        privateTrace.helper_id = helperId;
        report.cleanup.fixture_created = true;
        persistPrivate();
      }
      assert.ok(fixtureCreated, "fixture_creation_failed");

      const entities = await page.evaluate(() => document.querySelector("habab-setup-chat").hass.callWS({ type: "config/entity_registry/list" }));
      const entity = entities.find((item) => item.platform === "input_boolean" && item.unique_id === helperId);
      assert.ok(entity?.entity_id?.startsWith("input_boolean.habab_qa_service_call_20261024"), "fixture_entity_id_mismatch");
      entityId = entity.entity_id;
      privateTrace.entity_id = entityId;
      persistPrivate();
      const initial = await readState(runtime, entityId);
      assert.equal(initial?.friendly_name, FIXTURE_NAME, "fixture_name_mismatch");
      assert.equal(initial.state, "off", "fixture_initial_state_not_off");
      const ownerUserId = await page.evaluate(() => document.querySelector("habab-setup-chat").hass.user?.id);
      assert.ok(typeof ownerUserId === "string" && ownerUserId, "authenticated_user_id_missing");
      privateTrace.owner_user_id = ownerUserId;

      await page.evaluate(async (fixtureEntityId) => {
        const chat = document.querySelector("habab-setup-chat");
        window.__hababQaServiceCalls = [];
        window.__hababQaServiceUnsubscribe = await chat.hass.connection.subscribeEvents((event) => {
          const data = event.data || {};
          const serviceData = data.service_data || {};
          const target = data.target || {};
          const ids = [serviceData.entity_id, target.entity_id].flatMap((value) =>
            Array.isArray(value) ? value : typeof value === "string" ? value.split(",").map((item) => item.trim()) : []);
          window.__hababQaServiceCalls.push({
            domain: data.domain,
            service: data.service,
            entity_ids: [...new Set(ids)],
            targets_fixture: ids.includes(fixtureEntityId),
            context_user_id: event.context?.user_id || data.context?.user_id || null,
          });
        }, "call_service");
      }, entityId);
      unsubscribeInstalled = true;

      // Calibrate the event observer through native HA helper/service APIs.
      // These two direct calls are apparatus only, never counted as chat proof.
      async function calibrateNativeService(service, expectedState) {
        const offset = await page.evaluate(() => window.__hababQaServiceCalls.length);
        await page.evaluate(({ service, fixtureEntityId }) => document.querySelector("habab-setup-chat").hass.callService(
          "input_boolean", service, { entity_id: fixtureEntityId }), { service, fixtureEntityId: entityId });
        await page.waitForFunction((start) => window.__hababQaServiceCalls.length > start,
          { timeout: 10000 }, offset);
        const event = await page.evaluate((start) => window.__hababQaServiceCalls[start], offset);
        const state = await readState(runtime, entityId);
        assert.equal(event.domain, "input_boolean", "calibration_wrong_domain");
        assert.equal(event.service, service, "calibration_wrong_service");
        assert.deepEqual(event.entity_ids, [entityId], "calibration_wrong_target");
        assert.equal(event.context_user_id, ownerUserId, "calibration_context_mismatch");
        assert.equal(state?.state, expectedState, "calibration_readback_mismatch");
        privateTrace.calibration = privateTrace.calibration || [];
        privateTrace.calibration.push({ service, event, readback: state?.state });
        report.calibration = { observer_acknowledged: true, calls_observed: privateTrace.calibration.length,
          target_matched: true, context_matched: true, state_restored_off: expectedState === "off" };
        persistPrivate();
        savePublic();
      }
      await calibrateNativeService("turn_on", "on");
      await calibrateNativeService("turn_off", "off");

      async function sendTurn(name, prompt, expectedState, expectedService) {
        const start = await page.evaluate(() => {
          const chat = document.querySelector("habab-setup-chat");
          return { message_count: chat.messages.length, event_count: window.__hababQaServiceCalls.length };
        });
        privateTrace.prompts.push({ name, text: prompt });
        persistPrivate();
        chatTurnMayBeActive = true;
        await page.evaluate((text) => {
          const chat = document.querySelector("habab-setup-chat");
          const input = chat.shadowRoot.querySelector("textarea");
          input.focus();
          input.value = text;
          input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
          chat.shadowRoot.querySelector(".send").click();
        }, prompt);
        await page.waitForFunction(({ count }) => {
          const chat = document.querySelector("habab-setup-chat");
          const error = chat.shadowRoot.querySelector("#status")?.classList.contains("error");
          return !chat.busy && (chat.messages.length >= count + 2 || error);
        }, { timeout: 210000 }, { count: start.message_count });
        chatTurnMayBeActive = false;
        const reply = await page.evaluate(() => {
          const chat = document.querySelector("habab-setup-chat");
          return { role: chat.messages.at(-1)?.role, text: chat.messages.at(-1)?.text || "",
            error: chat.shadowRoot.querySelector("#status")?.classList.contains("error") || false };
        });
        privateTrace.replies.push({ name, text: reply.text });
        const events = await page.evaluate((offset) => window.__hababQaServiceCalls.slice(offset), start.event_count);
        privateTrace.call_service_events.push({ name, events });
        persistPrivate();
        assert.equal(reply.role, "assistant", `${name}_assistant_reply_missing_or_failed`);
        assert.equal(reply.error, false, `${name}_turn_ended_with_error`);

        if (expectedService) {
          assert.equal(events.length, 1, `${name}_expected_one_service_call`);
          assert.equal(events[0].domain, "input_boolean", `${name}_wrong_service_domain`);
          assert.equal(events[0].service, expectedService, `${name}_wrong_service`);
          assert.deepEqual(events[0].entity_ids, [entityId], `${name}_wrong_service_target`);
          assert.equal(events[0].targets_fixture, true, `${name}_fixture_target_missing`);
          assert.equal(events[0].context_user_id, ownerUserId, `${name}_context_mismatch`);
        } else {
          assert.equal(events.length, 0, "unknown_target_must_not_emit_service_call");
        }
        let state = await readState(runtime, entityId);
        const deadline = Date.now() + 20000;
        while (expectedState && state?.state !== expectedState && Date.now() < deadline) {
          await new Promise((resolve) => setTimeout(resolve, 500));
          state = await readState(runtime, entityId);
        }
        assert.equal(state?.friendly_name, FIXTURE_NAME, `${name}_fixture_missing`);
        assert.equal(state.state, expectedState, `${name}_readback_mismatch`);
        report.cases[name] = { assistant_replied: true, service_call_count: events.length,
          target_matched: expectedService ? events[0]?.targets_fixture === true : false,
          context_matched: expectedService ? events[0]?.context_user_id === ownerUserId : false,
          readback: state.state };
        savePublic();
      }

      await sendTurn("turn_on", `Включи только тестовый переключатель ${entityId}. Не меняй другие сущности и после вызова проверь его состояние.`, "on", "turn_on");
      await sendTurn("turn_off", `Выключи только тестовый переключатель ${entityId}. Не меняй другие сущности и после вызова проверь его состояние.`, "off", "turn_off");
      await sendTurn("unknown_target", "Включи устройство с названием «Несуществующая QA цель issue-monitor». Сначала проверь, есть ли оно в Home Assistant. Не выбирай похожее устройство и не выполняй действие, если точной цели нет.", "off", null);
      report.verdict = "ACTIONS_PASS_CLEANUP_PENDING";
      } finally {
        // withOwnedChromium closes the page after this callback resolves.
        await cleanupWhileBrowserOpen(page);
      }
    });
  } catch (error) {
    setupError = error instanceof Error ? error.message : "e2e_failed";
    if (report.verdict !== "FAIL_CLEANUP") report.verdict = "FAIL";
    report.failure_code = /^[a-z0-9_]+$/.test(setupError) ? setupError : "e2e_assertion_failed";
  } finally {
    try {
      savePublic();
    } catch {
      report.public_report_saved = false;
    }
  }
  process.stdout.write(JSON.stringify({ verdict: report.verdict, cases: report.cases, cleanup: report.cleanup }) + "\n");
  if (report.verdict !== "PASS") process.exitCode = 1;
}

async function pageHelperExists(page, helperId) {
  return page.evaluate(async (id) => {
    const helpers = await document.querySelector("habab-setup-chat").hass.callWS({ type: "input_boolean/list" });
    return helpers.some((helper) => helper.id === id);
  }, helperId);
}

main().catch((error) => {
  const code = error instanceof Error && /^[a-z0-9_]+$/.test(error.message)
    ? error.message : "preflight_failed";
  process.stderr.write(`${code}\n`);
  process.exitCode = 1;
});
