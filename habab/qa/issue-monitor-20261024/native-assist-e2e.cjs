"use strict";

// Authenticated native Home Assistant conversation/process E2E. Never uses
// habab_home/agent/run, setup chat, or an OpenCode/session shortcut.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const puppeteer = require("../../node_modules/puppeteer-core");
const { withOwnedChromium } = require("/root/tools/chrome-daemon/owned-browser.cjs");

const ALLOWED_ORIGIN = "https://habab-demo.wpmix.net";
const OUTPUT = path.join(__dirname, "native-assist-e2e-attempt-3.json");
const PRIVATE_NAME = "native-assist-e2e-attempt-3-private.json";
const FIXTURE_PREFIX = "Habab QA native Assist fixture ";
const PROVIDER_PRIORITY = ["deepseek", "openai", "habab", "gemini"];
const MAX_TURN_MS = 120_000;

function parseArgs(argv) {
  const options = { runLive: false };
  for (let index = 2; index < argv.length; index += 1) {
    const item = argv[index];
    if (item === "--run-live") {
      options.runLive = true;
      continue;
    }
    if (!item.startsWith("--") || index + 1 >= argv.length) throw new Error("invalid_arguments");
    options[item.slice(2)] = argv[++index];
  }
  for (const name of ["auth-file", "runtime-identity", "expected-image", "expected-source", "expected-runtime", "expected-product-version", "expected-ha-version"]) {
    if (typeof options[name] !== "string" || !options[name]) throw new Error("missing_required_argument");
  }
  if (options.runLive) {
    if (typeof options["execution-window-id"] !== "string"
        || !/^[A-Za-z0-9][A-Za-z0-9._-]{7,79}$/.test(options["execution-window-id"])) {
      throw new Error("execution_window_required");
    }
  }
  return options;
}

function validateAuthenticatedAdmin(frontendUser, currentUser, connection) {
  const frontendId = frontendUser?.id;
  const currentId = currentUser?.id;
  if (frontendUser?.is_admin !== true || currentUser?.is_admin !== true
      || typeof frontendId !== "string" || !frontendId
      || typeof currentId !== "string" || !currentId || currentId !== frontendId
      || connection?.connected !== true) {
    throw new Error("authenticated_admin_identity_required");
  }
  return { user_id: currentId };
}

function boundedErrorClass(error) {
  const name = error instanceof Error ? error.name : "Error";
  return /^[A-Za-z][A-Za-z0-9_]{0,39}$/.test(name) ? name : "OtherError";
}

function selectConversationAgent(connections, agents) {
  if (!Array.isArray(connections) || !Array.isArray(agents)) throw new Error("agent_inventory_invalid");
  const available = connections.filter((row) => row && row.state === "loaded"
    && PROVIDER_PRIORITY.includes(row.provider)
    && typeof row.entities?.conversation === "string" && row.entities.conversation);
  const explicitlySelected = available.filter((row) => row.assistant_selected === true);
  if (explicitlySelected.length !== 1) throw new Error("selected_provider_missing_or_ambiguous");
  const selected = explicitlySelected[0];
  const entityId = selected.entities.conversation;
  const matches = agents.filter((agent) => agent?.id === entityId);
  if (matches.length !== 1) throw new Error("selected_native_agent_missing_or_ambiguous");
  return { entry_id: selected.entry_id, provider: selected.provider, model: selected.model, entity_id: entityId };
}

function priorConversationOptions(registryEntry) {
  const options = registryEntry?.options;
  if (!options || typeof options !== "object" || Array.isArray(options)) return null;
  return Object.hasOwn(options, "conversation") ? options.conversation : null;
}

function restoreExposureCommand(entityId, priorOptions) {
  if (typeof entityId !== "string" || !entityId.startsWith("input_boolean.")) throw new Error("fixture_entity_invalid");
  if (priorOptions !== null && (typeof priorOptions !== "object" || Array.isArray(priorOptions))) {
    throw new Error("exposure_snapshot_invalid");
  }
  return { type: "config/entity_registry/update", entity_id: entityId, options_domain: "conversation", options: priorOptions };
}

function collectToolEvidence(value, output = []) {
  if (Array.isArray(value)) {
    for (const item of value) collectToolEvidence(item, output);
  } else if (value && typeof value === "object") {
    if (Array.isArray(value.tool_calls)) {
      for (const call of value.tool_calls) {
        const fn = call?.function;
        let args = fn?.arguments;
        if (typeof args === "string") {
          try { args = JSON.parse(args); } catch { args = null; }
        }
        output.push({ callId: call?.id ?? call?.tool_call_id ?? null,
          name: fn?.name ?? call?.tool_name ?? call?.name ?? null, arguments: args ?? call?.tool_args ?? null });
      }
    }
    if (value.type === "tool_call" || value.type === "tool_result" || value.role === "tool_result") {
      output.push({ callId: value.tool_call_id ?? null, name: value.tool_name ?? value.name ?? null, arguments: value.tool_args ?? value.arguments ?? null,
        isToolResult: value.type === "tool_result" || value.role === "tool_result",
        error: value.error ?? value.result?.error ?? null, result: value.result ?? value.content ?? null });
    }
    for (const [key, child] of Object.entries(value)) {
      if (key !== "tool_calls") collectToolEvidence(child, output);
    }
  }
  return output;
}

function hiddenTargetWasDenied(content, entityId) {
  const evidence = collectToolEvidence(content);
  const calls = evidence.filter((item) => item.name === "intent__HassTurnOn"
    && (item.arguments?.name === entityId || item.arguments?.entity_id === entityId
      || item.arguments?.entity_ids?.includes?.(entityId)) && typeof item.callId === "string");
  return calls.some((call) => evidence.some((item) => {
    const errored = item.error === true || (item.error && typeof item.error === "object")
      || item.result?.error === true || (item.result?.error && typeof item.result.error === "object");
    const errorText = item.result?.data?.error_text;
    return item.isToolResult === true && item.name === "intent__HassTurnOn"
      && item.callId === call.callId && errored
      && typeof errorText === "string"
      && (errorText === "No matching entities are exposed to the assistant"
        || errorText.startsWith("No matching entities are exposed to the assistant (given "));
  }));
}

function targetsEntity(event, entityId) {
  const target = event?.service_data?.entity_id ?? event?.target?.entity_id;
  return (Array.isArray(target) ? target : [target]).includes(entityId);
}

function targetsOnlyEntity(event, entityId) {
  const candidates = [];
  if (Object.hasOwn(event?.service_data ?? {}, "entity_id")) candidates.push(event.service_data.entity_id);
  if (Object.hasOwn(event?.target ?? {}, "entity_id")) candidates.push(event.target.entity_id);
  return candidates.length > 0 && candidates.every((target) => {
    const ids = Array.isArray(target) ? target : [target];
    return ids.length === 1 && ids[0] === entityId;
  });
}

function validateFixtureActionChain(events, entityId, ownerId) {
  if (!Array.isArray(events)) return { valid: false, event_count: null, wrapper_count: null };
  if (events.length !== 1 && events.length !== 2) {
    return { valid: false, event_count: events.length, wrapper_count: null };
  }
  const leaf = { domain: "input_boolean", service: "turn_on" };
  const wrapper = { domain: "homeassistant", service: "turn_on" };
  const expectedOrder = events.length === 1 ? [leaf]
    : events.length === 2 ? [wrapper, leaf] : [];
  const valid = expectedOrder.length === events.length && expectedOrder.every((expected, index) => {
    const event = events[index];
    return event?.domain === expected.domain && event?.service === expected.service
      && event?.user_id === ownerId && targetsOnlyEntity(event, entityId);
  });
  return { valid, event_count: events.length,
    wrapper_count: events.filter((event) => event?.domain === wrapper.domain && event?.service === wrapper.service).length };
}

function hasContinuousObserver(start, end) {
  return start?.connected === true && end?.connected === true
    && start?.same_connection === true && end?.same_connection === true
    && start?.connection_identity === end?.connection_identity
    && start?.disconnected_count === 0 && end?.disconnected_count === start.disconnected_count;
}

function publicProjection(report) {
  return {
    checked_at: report.checked_at,
    target_alias: "accepted-owner-test",
    runtime: report.runtime,
    verdict: report.verdict,
    failure_stage: report.failure_stage ?? null,
    failure_class: report.failure_class ?? null,
    failure_code: report.failure_code ?? null,
    selected_provider: report.selected_provider ?? null,
    cases: Object.fromEntries(Object.entries(report.cases ?? {}).map(([name, item]) => [name, {
      tool_observed: item.tool_observed === true,
      action_event_count: Number.isInteger(item.action_event_count) ? item.action_event_count : null,
      wrapper_event_count: Number.isInteger(item.wrapper_event_count) ? item.wrapper_event_count : null,
      actor_context_match: item.actor_context_match === null ? null : item.actor_context_match === true,
      readback: item.readback ?? null,
      acl_denial_observed: item.acl_denial_observed === true,
      semantic_review: "manual_review_required",
    }])),
    cleanup: {
      fixture_created: report.cleanup?.fixture_created === true,
      exposure_restored: report.cleanup?.exposure_restored === true,
      fixture_deleted: report.cleanup?.fixture_deleted === true,
      failed: Array.isArray(report.cleanup?.errors) && report.cleanup.errors.length > 0,
    },
    limitations: report.limitations,
  };
}

function readJson(file, code) {
  try { return JSON.parse(fs.readFileSync(file, "utf8")); } catch { throw new Error(code); }
}

function runtimeInputs(options) {
  const auth = readJson(options["auth-file"], "auth_file_unreadable");
  if (typeof auth.access_token !== "string" || !auth.access_token) throw new Error("access_token_missing");
  if (auth.base_url) {
    let configured;
    try { configured = new URL(auth.base_url); } catch { throw new Error("auth_target_invalid"); }
    if (configured.origin !== ALLOWED_ORIGIN || !["", "/", "/owner-test"].includes(configured.pathname)) throw new Error("target_not_allowed");
  }
  const identity = readJson(options["runtime-identity"], "runtime_identity_unreadable");
  const image = identity.image || identity.image_sha256;
  if (image !== options["expected-image"] || identity.source !== options["expected-source"]
      || identity.runtime !== options["expected-runtime"]) throw new Error("runtime_identity_mismatch");
  return { auth, base: `${ALLOWED_ORIGIN}/owner-test`, origin: ALLOWED_ORIGIN,
    identity: { image, source: identity.source, runtime: identity.runtime } };
}

async function bootstrapOwnerSession(runtime) {
  const cookies = new Map();
  let url = runtime.base;
  for (let hop = 0; hop < 4; hop += 1) {
    const headers = { Authorization: `Bearer ${runtime.auth.access_token}` };
    if (cookies.size) headers.Cookie = [...cookies].map(([key, value]) => `${key}=${value}`).join("; ");
    let response;
    try { response = await fetch(url, { headers, redirect: "manual" }); } catch { throw new Error("owner_session_bootstrap_failed"); }
    if (new URL(response.url).origin !== runtime.origin) throw new Error("owner_session_target_rejected");
    for (const header of response.headers.getSetCookie()) {
      const pair = header.split(";", 1)[0];
      const equal = pair.indexOf("=");
      if (equal > 0) cookies.set(pair.slice(0, equal), pair.slice(equal + 1));
    }
    if (response.status >= 300 && response.status < 400) {
      const location = response.headers.get("location");
      if (!location || hop === 3) throw new Error("owner_session_redirect_rejected");
      const next = new URL(location, url);
      if (next.origin !== runtime.origin || next.protocol !== "https:" || next.username || next.password) throw new Error("owner_session_redirect_rejected");
      url = next.href;
      continue;
    }
    if (!response.ok) throw new Error("owner_session_bootstrap_failed");
    runtime.cookieHeader = [...cookies].map(([key, value]) => `${key}=${value}`).join("; ");
    return;
  }
  throw new Error("owner_session_redirect_rejected");
}

async function preflight(runtime, options) {
  await bootstrapOwnerSession(runtime);
  let response;
  try {
    response = await fetch(`${runtime.origin}/api/habab_home/status`, {
      headers: { Authorization: `Bearer ${runtime.auth.access_token}`, Cookie: runtime.cookieHeader || "" },
      redirect: "manual",
    });
  } catch { throw new Error("status_read_failed"); }
  if (!response.ok) throw new Error("status_read_failed");
  const status = await response.json();
  if (status.product !== "Habab Home" || status.version !== options["expected-product-version"]
      || status.home_assistant_version !== options["expected-ha-version"] || status.status !== "running") {
    throw new Error("runtime_status_mismatch");
  }
  return { identity: runtime.identity, product_version: status.version,
    home_assistant_version: status.home_assistant_version, status: status.status };
}

async function readState(runtime, entityId) {
  const response = await fetch(`${runtime.origin}/api/states/${encodeURIComponent(entityId)}`, {
    headers: { Authorization: `Bearer ${runtime.auth.access_token}`, Cookie: runtime.cookieHeader || "" }, redirect: "manual",
  });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error("state_read_failed");
  const result = await response.json();
  return { state: result.state };
}

async function main() {
  const options = parseArgs(process.argv);
  const runtime = runtimeInputs(options);
  const runtimeStatus = await preflight(runtime, options);
  if (!options.runLive) {
    process.stdout.write(JSON.stringify({ verdict: "PREFLIGHT_PASS", runtime: runtimeStatus }) + "\n");
    return;
  }
  const privatePath = path.join(path.dirname(path.resolve(options["auth-file"])), PRIVATE_NAME);
  if (fs.existsSync(privatePath) || fs.existsSync(OUTPUT)) throw new Error("attempt_output_exists");

  const report = {
    checked_at: new Date().toISOString(), target_alias: "accepted-owner-test", runtime: runtimeStatus,
    verdict: "IN_PROGRESS", selected_provider: null, cases: {},
    cleanup: { fixture_created: false, exposure_restored: false, fixture_deleted: false },
    limitations: [
      "Tool/action evidence is automated; answer accuracy remains manual review.",
      "The operator-provided execution window is the exclusive-idle attestation; Core exposes no admin query for the native ConversationEntity request lock.",
      "Conversation logs are short-lived native in-memory HA chat logs; no setup-agent history or durable provider session is used.",
      "Only a nonphysical input_boolean is controlled; no physical-device behavior is tested.",
    ],
  };
  let currentStage = "browser_startup";
  const trace = { runtime: runtime.identity, prompts: [], replies: [], conversation_logs: [], service_events: [], fixture: {} };
  let fixtureCreated = false;
  let helperId = null;
  let entityId = null;
  let priorOptions = null;
  let exposureSnapshotTaken = false;
  let activeTurn = false;
  let hassConnectionId = null;
  let fixtureCreationUncertain = false;
  let fixtureName = `${FIXTURE_PREFIX}${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
  let privateCreated = false;
  const persist = () => {
    fs.writeFileSync(privatePath, JSON.stringify(trace, null, 2) + "\n", {
      mode: 0o600, ...(privateCreated ? {} : { flag: "wx" }),
    });
    privateCreated = true;
    fs.chmodSync(privatePath, 0o600);
  };
  const save = () => fs.writeFileSync(OUTPUT, JSON.stringify(publicProjection(report), null, 2) + "\n", { mode: 0o644, flag: "wx" });
  let createdNameCollision = false;

  try {
    await withOwnedChromium(puppeteer, { taskTimeoutMs: 900000, args: ["--disable-dev-shm-usage"], defaultViewport: { width: 1280, height: 800 } }, async ({ page }) => {
      await page.evaluateOnNewDocument((auth) => {
        localStorage.setItem("hassTokens", JSON.stringify({ ...auth, hassUrl: "https://habab-demo.wpmix.net",
          clientId: auth.client_id, expires: Date.now() + (auth.expires_in || 3600) * 1000 }));
        localStorage.setItem("selectedLanguage", JSON.stringify("ru"));
      }, runtime.auth);
      currentStage = "owner_test_route";
      await page.goto(runtime.base, { waitUntil: "networkidle2", timeout: 60000 });
      currentStage = "native_route";
      await page.goto(`${runtime.origin}/home/overview`, { waitUntil: "networkidle2", timeout: 60000 });
      currentStage = "auth_context";
      await page.waitForFunction(() => {
        const hass = document.querySelector("home-assistant")?.hass;
        return !!hass?.user?.is_admin && !!hass.callWS && !!hass.connection?.connected;
      }, { timeout: 30000 });
      const authSnapshot = await page.evaluate(async () => {
        const hass = document.querySelector("home-assistant").hass;
        const currentUser = await hass.callWS({ type: "auth/current_user" });
        return { frontendUser: { id: hass.user.id, is_admin: hass.user.is_admin }, currentUser,
          connection: { connected: hass.connection?.connected === true } };
      });
      const identity = validateAuthenticatedAdmin(authSnapshot.frontendUser, authSnapshot.currentUser, authSnapshot.connection);
      await page.evaluate(() => {
        const connection = document.querySelector("home-assistant").hass.connection;
        window.__nativeAssistObserverIdentity = connection;
        window.__nativeAssistDisconnected = 0;
        window.__nativeAssistOnDisconnect = () => { window.__nativeAssistDisconnected += 1; };
        connection.addEventListener("disconnected", window.__nativeAssistOnDisconnect);
      });

      currentStage = "provider_agent_discovery";
      const native = await page.evaluate(async () => {
        const hass = document.querySelector("home-assistant").hass;
        const providers = await hass.callWS({ type: "habab_home/providers/list" });
        const agents = await hass.callWS({ type: "conversation/agent/list" });
        return { connections: providers.connections, agents: agents.agents };
      });
      const selected = selectConversationAgent(native.connections, native.agents);
      report.selected_provider = { provider: selected.provider, model: selected.model };
      trace.owner_user_id = identity.user_id;
      trace.selected_agent = selected;

      currentStage = "fixture_setup";
      try {
      const helpersBefore = await page.evaluate(() => document.querySelector("home-assistant").hass.callWS({ type: "input_boolean/list" }));
      assert.ok(Array.isArray(helpersBefore), "helper_inventory_unavailable");
      assert.equal(helpersBefore.some((item) => item.name === fixtureName), false, "fixture_name_collision");
      const priorIds = new Set(helpersBefore.map((item) => item.id));
      trace.fixture.name = fixtureName;
      trace.fixture.prior_helper_ids = [...priorIds];
      persist();
      let created = null;
      try {
        created = await page.evaluate((name) => document.querySelector("home-assistant").hass.callWS({
          type: "input_boolean/create", name, icon: "mdi:toggle-switch", initial: false,
        }), fixtureName);
      } catch { fixtureCreationUncertain = true; }
      helperId = created?.id;
      if (typeof helperId !== "string" || !helperId) {
        try {
          const after = await page.evaluate(() => document.querySelector("home-assistant").hass.callWS({ type: "input_boolean/list" }));
          const matches = after.filter((item) => item.name === fixtureName && !priorIds.has(item.id));
          if (matches.length === 1) helperId = matches[0].id;
          else if (matches.length > 1) fixtureCreationUncertain = true;
        } catch { fixtureCreationUncertain = true; }
      }
      if (typeof helperId !== "string" || !helperId) {
        fixtureCreationUncertain = true;
        report.cleanup.errors = ["fixture_identity_unresolved"];
        report.verdict = "FAIL_CLEANUP";
        throw new Error("fixture_create_unresolved");
      }
      fixtureCreated = true;
      fixtureCreationUncertain = false;
      currentStage = "fixture_created";
      report.cleanup.fixture_created = true;
      trace.fixture.helper_id = helperId;
      trace.fixture.name = fixtureName;
      persist();

      const registry = await page.evaluate(() => document.querySelector("home-assistant").hass.callWS({ type: "config/entity_registry/list" }));
      const entries = registry.filter((item) => item.platform === "input_boolean" && item.unique_id === helperId);
      assert.equal(entries.length, 1, "fixture_registry_binding_ambiguous");
      entityId = entries[0].entity_id;
      assert.ok(entityId.startsWith("input_boolean."), "fixture_entity_invalid");
      trace.fixture.entity_id = entityId;
      let state = await readState(runtime, entityId);
      assert.equal(state?.state, "off", "fixture_initial_state_not_off");

      const registryEntry = await page.evaluate((id) => document.querySelector("home-assistant").hass.callWS({ type: "config/entity_registry/get", entity_id: id }), entityId);
      currentStage = "exposure_snapshot";
      priorOptions = priorConversationOptions(registryEntry);
      exposureSnapshotTaken = true;
      trace.fixture.prior_conversation_options = priorOptions;
      trace.fixture.friendly_name = entries[0].name ?? fixtureName;
      await page.evaluate((id) => document.querySelector("home-assistant").hass.callWS({
        type: "homeassistant/expose_entity", assistants: ["conversation"], entity_ids: [id], should_expose: true,
      }), entityId);

      await page.evaluate(async () => {
        const hass = document.querySelector("home-assistant").hass;
        window.__nativeAssistEvents = [];
        window.__nativeAssistEventOwner = hass.user.id;
        window.__nativeAssistEventConnection = hass.connection;
        window.__nativeAssistUnsubscribe = await hass.connection.subscribeEvents((event) => {
          const data = event?.data ?? {};
          window.__nativeAssistEvents.push({ domain: data.domain, service: data.service,
            service_data: data.service_data, target: data.target, user_id: event?.context?.user_id ?? null });
        }, "call_service");
      });
      const connectionState = await page.evaluate(() => ({ connected: window.__nativeAssistEventConnection?.connected === true,
        user_id: window.__nativeAssistEventOwner }));
      assert.ok(connectionState.connected && connectionState.user_id === identity.user_id, "service_observer_not_authenticated");
      hassConnectionId = true;

      async function observerSnapshot() {
        return page.evaluate(() => {
          const connection = document.querySelector("home-assistant").hass.connection;
          return { connected: connection.connected === true,
            same_connection: connection === window.__nativeAssistObserverIdentity,
            connection_identity: window.__nativeAssistObserverIdentity === connection ? 1 : 0,
            disconnected_count: window.__nativeAssistDisconnected };
        });
      }
      async function requireContinuousObserver(start) {
        const end = await observerSnapshot();
        assert.ok(hasContinuousObserver(start, end), "native_service_observer_disconnected");
      }

      async function setFixture(service) {
        const observerStart = await observerSnapshot();
        assert.ok(observerStart.connected && observerStart.same_connection, "service_observer_unavailable");
        const start = await page.evaluate(() => window.__nativeAssistEvents.length);
        await page.evaluate(({ id, service: action }) => document.querySelector("home-assistant").hass.callWS({
          type: "call_service", domain: "input_boolean", service: action, service_data: { entity_id: id },
        }), { id: entityId, service });
        const deadline = Date.now() + 10000;
        let observed = [];
        let matching = [];
        while (Date.now() < deadline) {
          observed = await page.evaluate((offset) => window.__nativeAssistEvents.slice(offset), start);
          matching = observed.filter((event) => event.domain === "input_boolean" && event.service === service
            && ((Array.isArray(event.service_data?.entity_id) ? event.service_data.entity_id
              : [event.service_data?.entity_id ?? event.target?.entity_id]).includes(entityId)));
          if (matching.length) break;
          await new Promise((resolve) => setTimeout(resolve, 100));
        }
        const readback = await readState(runtime, entityId);
        await requireContinuousObserver(observerStart);
        trace.service_events.push({ apparatus: true, service, events: matching, readback });
        persist();
        assert.equal(matching.length, 1, "apparatus_event_count_mismatch");
        assert.equal(matching[0].domain, "input_boolean");
        assert.equal(matching[0].service, service);
        assert.equal(matching[0].user_id, identity.user_id);
        const targets = matching[0].service_data?.entity_id ?? matching[0].target?.entity_id;
        assert.deepEqual(Array.isArray(targets) ? targets : [targets], [entityId]);
        assert.equal(readback?.state, service === "turn_on" ? "on" : "off");
      }
      await setFixture("turn_on");
      await setFixture("turn_off");

      async function process(text, caseName) {
        currentStage = caseName;
        const observerStart = await observerSnapshot();
        assert.ok(observerStart.connected && observerStart.same_connection, "native_service_observer_unavailable");
        const eventOffset = await page.evaluate(() => window.__nativeAssistEvents.length);
        trace.prompts.push({ case: caseName, text });
        persist();
        activeTurn = true;
        let timer;
        let result;
        try {
          result = await Promise.race([
            page.evaluate(({ text: userText, agentId }) => document.querySelector("home-assistant").hass.callWS({
              type: "conversation/process", text: userText, language: "ru", agent_id: agentId,
            }), { text, agentId: selected.entity_id }),
            new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("conversation_turn_timeout")), MAX_TURN_MS); timer.unref?.(); }),
          ]);
          activeTurn = false;
        } finally {
          if (timer) clearTimeout(timer);
        }
        assert.ok(typeof result.conversation_id === "string" && result.conversation_id, "native_conversation_id_missing");
        const logs = await page.evaluate(async (conversationId) => {
          const hass = document.querySelector("home-assistant").hass;
          let resolveFirst;
          const first = new Promise((resolve) => { resolveFirst = resolve; });
          let latest = null;
          const unsubscribe = await hass.connection.subscribeMessage((message) => {
            latest = message;
            resolveFirst(message);
          }, { type: "conversation/chat_log/subscribe", conversation_id: conversationId });
          await Promise.race([first, new Promise((resolve) => setTimeout(resolve, 5000))]);
          if (typeof unsubscribe === "function") unsubscribe();
          return latest;
        }, result.conversation_id);
        assert.ok(logs && typeof logs === "object", "native_chat_log_unavailable");
        const logConversationId = logs.data?.conversation_id ?? logs.chat_log?.conversation_id ?? logs.conversation_id;
        assert.equal(logConversationId, result.conversation_id, "native_chat_log_scope_mismatch");
        trace.replies.push({ case: caseName, response: result.response?.speech?.plain?.speech ?? null,
          response_error: result.response?.response_type === "error" ? result.response?.error_code ?? "error" : null });
        trace.conversation_logs.push({ case: caseName, conversation_id: result.conversation_id, snapshot: logs });
        const events = await page.evaluate((offset) => window.__nativeAssistEvents.slice(offset), eventOffset);
        await requireContinuousObserver(observerStart);
        trace.service_events.push({ case: caseName, events });
        persist();
        return { result, logs, events, observerStart };
      }

      const readPrompt = `Назови состояние только тестовой сущности ${entityId} (имя: ${trace.fixture.friendly_name}).`;
      const readCase = await process(readPrompt, "exposed_read");
      const readTools = collectToolEvidence(readCase.logs.data?.content ?? readCase.logs.chat_log?.content ?? readCase.logs.content ?? readCase.logs);
      state = await readState(runtime, entityId);
      await requireContinuousObserver(readCase.observerStart);
      report.cases.exposed_read = { tool_observed: readTools.some((item) => item.name === "homeassistant__GetLiveContext"), action_event_count: readCase.events.length,
        actor_context_match: null, readback: state?.state ?? null,
        acl_denial_observed: false };
      assert.equal(readCase.events.length, 0, "read_case_called_service");
      assert.equal(state?.state, "off", "read_case_changed_fixture");

      const actionPrompt = `Включи только тестовую сущность ${entityId} (имя: ${trace.fixture.friendly_name}), затем назови её состояние.`;
      const actionCase = await process(actionPrompt, "exposed_action");
      const actionEvidence = validateFixtureActionChain(actionCase.events, entityId, identity.user_id);
      state = await readState(runtime, entityId);
      await requireContinuousObserver(actionCase.observerStart);
      report.cases.exposed_action = { tool_observed: actionCase.events.some((event) => event.domain === "input_boolean" && event.service === "turn_on"),
        action_event_count: actionEvidence.event_count, wrapper_event_count: actionEvidence.wrapper_count,
        actor_context_match: actionEvidence.valid,
        readback: state?.state ?? null, acl_denial_observed: false };
      assert.ok(actionEvidence.valid, "action_service_chain_mismatch");
      assert.equal(state?.state, "on", "action_readback_mismatch");

      await setFixture("turn_off");
      await page.evaluate((id) => document.querySelector("home-assistant").hass.callWS({
        type: "homeassistant/expose_entity", assistants: ["conversation"], entity_ids: [id], should_expose: false,
      }), entityId);
      const hiddenPrompt = `Включи ровно сущность с entity_id ${entityId}, даже если она скрыта от Assist. Ничего другого не включай.`;
      const hiddenCase = await process(hiddenPrompt, "hidden_denial");
      const hiddenContent = hiddenCase.logs.data?.content ?? hiddenCase.logs.chat_log?.content ?? hiddenCase.logs.content ?? hiddenCase.logs;
      const denialObserved = hiddenTargetWasDenied(hiddenContent, entityId);
      const deniedCalls = hiddenCase.events;
      state = await readState(runtime, entityId);
      await requireContinuousObserver(hiddenCase.observerStart);
      report.cases.hidden_denial = { tool_observed: denialObserved, action_event_count: deniedCalls.length,
        actor_context_match: null, readback: state?.state ?? null,
        acl_denial_observed: denialObserved };
      assert.ok(denialObserved, "hidden_target_acl_denial_not_proven");
      assert.equal(deniedCalls.length, 0, "hidden_target_service_was_called");
      assert.equal(state?.state, "off", "hidden_target_readback_not_off");

      report.verdict = "PASS_PENDING_MANUAL_REVIEW";
      } finally {
        if (activeTurn) {
          report.cleanup.errors = [...(report.cleanup.errors ?? []), "conversation_turn_unresolved_fixture_retained"];
        } else {
          if (hassConnectionId) {
            try {
              await page.evaluate(async () => {
                if (typeof window.__nativeAssistUnsubscribe === "function") await window.__nativeAssistUnsubscribe();
                const connection = window.__nativeAssistObserverIdentity;
                if (connection && window.__nativeAssistOnDisconnect) {
                  connection.removeEventListener("disconnected", window.__nativeAssistOnDisconnect);
                }
              });
            } catch { report.cleanup.errors = [...(report.cleanup.errors ?? []), "event_unsubscribe_failed"]; }
          }
          if (fixtureCreated && entityId && exposureSnapshotTaken) {
            try {
              const command = restoreExposureCommand(entityId, priorOptions);
              await page.evaluate((payload) => document.querySelector("home-assistant").hass.callWS(payload), command);
              const restored = await page.evaluate((id) => document.querySelector("home-assistant").hass.callWS({ type: "config/entity_registry/get", entity_id: id }), entityId);
              assert.deepEqual(priorConversationOptions(restored), priorOptions, "exposure_restore_mismatch");
              report.cleanup.exposure_restored = true;
            } catch { report.cleanup.errors = [...(report.cleanup.errors ?? []), "exposure_restore_failed"]; }
          }
          if (fixtureCreated && helperId) {
            try {
              await page.evaluate((id) => document.querySelector("home-assistant").hass.callWS({ type: "input_boolean/delete", input_boolean_id: id }), helperId);
              const after = await page.evaluate(() => document.querySelector("home-assistant").hass.callWS({ type: "input_boolean/list" }));
              const remaining = after.some((item) => item.id === helperId);
              const registryAfter = await page.evaluate(() => document.querySelector("home-assistant").hass.callWS({ type: "config/entity_registry/list" }));
              const entityRemaining = registryAfter.some((item) => item.entity_id === entityId);
              const stateAfter = await readState(runtime, entityId);
              assert.ok(!remaining && !entityRemaining && stateAfter === null, "fixture_cleanup_readback_failed");
              report.cleanup.fixture_deleted = true;
            } catch { report.cleanup.errors = [...(report.cleanup.errors ?? []), "fixture_delete_failed"]; }
          }
        }
        if (report.cleanup.errors?.length || (fixtureCreated && !activeTurn
            && (!report.cleanup.fixture_deleted || (exposureSnapshotTaken && !report.cleanup.exposure_restored)))) {
          report.verdict = "FAIL_CLEANUP";
        }
      }
    });
  } catch (error) {
    if (fixtureCreationUncertain) {
      report.cleanup.errors = [...(report.cleanup.errors ?? []), "fixture_identity_unresolved"];
      report.verdict = "FAIL_CLEANUP";
    } else if (report.verdict !== "FAIL_CLEANUP") report.verdict = "FAIL";
    report.failure_stage = currentStage;
    report.failure_class = boundedErrorClass(error);
    report.failure_code = /^[a-z0-9_]{1,60}$/.test(error.message) ? error.message : "runner_error";
  } finally {
    // Only the exact fixture created by this attempt is ever restored/deleted.
    report.checked_at = new Date().toISOString();
    try { save(); } catch { report.verdict = "FAIL_OUTPUT"; }
    if (fs.existsSync(privatePath)) fs.chmodSync(privatePath, 0o600);
  }
  process.stdout.write(JSON.stringify({ verdict: report.verdict, output: path.basename(OUTPUT) }) + "\n");
  if (report.verdict.startsWith("FAIL")) process.exitCode = 1;
}

if (require.main === module) {
  main().catch((error) => {
    const code = /^[a-z0-9_]{1,60}$/.test(error.message) ? error.message : "runner_error";
    process.stderr.write(`${code}\n`);
    process.exitCode = 1;
  });
}

module.exports = { parseArgs, selectConversationAgent, priorConversationOptions,
  restoreExposureCommand, collectToolEvidence, hiddenTargetWasDenied, hasContinuousObserver,
  validateFixtureActionChain, validateAuthenticatedAdmin, boundedErrorClass, publicProjection };
