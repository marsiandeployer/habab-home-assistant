"use strict";

// Real web-chat E2E for one disposable input_boolean and a read-only inventory case.
// Never uses setup_actions as a test surrogate: the browser chat sends every prompt.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const puppeteer = require("../../node_modules/puppeteer-core");
const WebSocket = require("../../node_modules/ws");
const { withOwnedChromium } = require("/root/tools/chrome-daemon/owned-browser.cjs");

// A later separately authorized run must receive a deliberately new unique label.
const ATTEMPT_LABEL = "20261025-attempt-5";
const OUTPUT = path.join(__dirname, `service-call-e2e-${ATTEMPT_LABEL}.json`);
const FIXTURE_NAME = "Habab QA service-call fixture 20261024";
const PRIVATE_TRACE_NAME = `service-call-e2e-${ATTEMPT_LABEL}-private.json`;
const ALLOWED_ORIGIN = "https://habab-demo.wpmix.net";
const FIRST_PUBLIC_OUTPUT = path.join(__dirname, "service-call-e2e.json");
const FIRST_PRIVATE_TRACE_NAME = "service-call-e2e-private.json";
const NATIVE_WS_URL = "ws://127.0.0.1:8395/api/websocket";

function hasAcceptedTurn(messages, prompt, probe, currentUi) {
  const userMessage = messages.at(-2);
  const assistantMessage = messages.at(-1);
  return probe?.accepted === true && probe?.completed === true && probe?.final_busy === false
    && probe.submitted_text === prompt && probe.expected_text === prompt
    && probe.status_error !== true
    && currentUi?.busy === false && currentUi?.status_error === false
    && userMessage?.role === "user" && userMessage.text === prompt
    && assistantMessage?.role === "assistant"
    && typeof assistantMessage.text === "string" && assistantMessage.text.trim().length > 0;
}

function shouldDeferFixtureCleanup(turnMayBeActive, backendActive) {
  return turnMayBeActive && backendActive !== false;
}

function hasContinuousObserver(start, end) {
  return Number.isInteger(start?.connection_identity) && start.connection_identity > 0
    && start.connection_identity === end?.connection_identity
    && start.disconnected_count === 0
    && end.disconnected_count === start.disconnected_count
    && start.same_connection === true && end.same_connection === true
    && start.connected === true && end.connected === true
    && start.owner_matches === true && end.owner_matches === true;
}

function observedEventsIfContinuous(start, end, events) {
  return hasContinuousObserver(start, end) && Array.isArray(events) ? events : null;
}

function hasSameDocument(start, end) {
  return typeof start?.document_marker === "string" && Boolean(start.document_marker)
    && start.document_marker === end?.document_marker
    && typeof start.main_frame_id === "string" && start.main_frame_id === end?.main_frame_id
    && typeof start.main_loader_id === "string" && start.main_loader_id === end?.main_loader_id
    && start.pathname === end?.pathname
    && Number.isInteger(start.chat_instance_id) && start.chat_instance_id === end?.chat_instance_id
    && start.same_host_as_baseline === true && end.same_host_as_baseline === true
    && start.owner_user_id === end?.owner_user_id
    && start.session_id === end?.session_id;
}

function hasNoDocumentNavigation(events) {
  const navigationKinds = new Set([
    "cdp_document_request", "cdp_navigated_within_document",
    "puppeteer_domcontentloaded", "puppeteer_load", "js_contexts_cleared",
    "document_created", "document_domcontentloaded", "pagehide", "beforeunload",
  ]);
  return Array.isArray(events) && events.every((event) => !navigationKinds.has(event.kind)
    && !(event.kind === "cdp_frame_navigated" && event.main === true)
    && !(event.kind === "puppeteer_frame_navigated" && event.main === true));
}

function hasCompletedSendCallbacks(events, caseId, baseline) {
  const current = events.filter((event) => event.case_id === caseId);
  const started = current.filter((event) => event.kind === "send_started");
  const accepted = current.filter((event) => event.kind === "send_accepted");
  const completed = current.filter((event) => event.kind === "send_completed");
  return started.length === 1 && accepted.length === 1 && completed.length === 1
    && accepted[0].accepted === true && completed[0].completed === true
    && !completed[0].error_class
    && completed[0].final_busy === false
    && [started[0], accepted[0], completed[0]].every((event) =>
      event.document_marker === baseline.document_marker
      && event.chat_instance_id === baseline.chat_instance_id);
}

function serviceEventProjection(event, sequence) {
  const data = event?.data || {};
  const target = data.target || {};
  const serviceData = data.service_data || {};
  const collect = (key) => [...new Set([serviceData[key], target[key]].flatMap((value) =>
    Array.isArray(value) ? value : typeof value === "string"
      ? value.split(",").map((item) => item.trim()).filter(Boolean) : []))];
  return {
    sequence,
    event_type: event?.event_type || null,
    domain: data.domain || null,
    service: data.service || null,
    entity_ids: collect("entity_id"),
    device_ids: collect("device_id"),
    area_ids: collect("area_id"),
    floor_ids: collect("floor_id"),
    label_ids: collect("label_id"),
    target_fields: Object.keys(target).sort(),
    context_user_id: event?.context?.user_id || data.context?.user_id || null,
    context_id: event?.context?.id || data.context?.id || null,
  };
}

function serviceEventTargetsOnlyEntity(event, entityId) {
  return event?.entity_ids?.length === 1 && event.entity_ids[0] === entityId
    && ["device_ids", "area_ids", "floor_ids", "label_ids"].every((key) => event[key]?.length === 0)
    && event.target_fields.every((key) => key === "entity_id");
}

async function readBackendDiagnostics(observer, sessionId) {
  let timer;
  try {
    return await Promise.race([
      observer.command({ type: "habab_home/agent/diagnostics", session_id: sessionId }, { timeoutMs: 5000 })
        .then((result) => ({ active: typeof result?.active === "boolean" ? result.active : null,
          error: result?.error || null })),
      new Promise((resolve) => {
        timer = setTimeout(() => resolve({ active: null, error: "diagnostics_timeout" }), 5100);
      }),
    ]);
  } catch {
    return { active: null, error: "diagnostics_unavailable" };
  } finally {
    clearTimeout(timer);
  }
}

class NativeHaServiceObserver {
  constructor(accessToken) {
    this.connectionIdentity = crypto.randomInt(1, Number.MAX_SAFE_INTEGER);
    this.connected = false;
    this.ownerMatches = false;
    this.disconnectedCount = 0;
    this.transportError = false;
    this.closedByUs = false;
    this.nextId = 1;
    this.pending = new Map();
    this.eventHandlers = new Map();
    this.events = [];
    this.ws = new WebSocket(NATIVE_WS_URL);
    this.ready = new Promise((resolve, reject) => {
      let timer = setTimeout(() => reject(new Error("native_ws_auth_timeout")), 15000);
      const fail = (code) => {
        clearTimeout(timer);
        timer = null;
        reject(new Error(code));
      };
      this.ws.on("message", (raw) => {
        let message;
        try { message = JSON.parse(raw.toString()); } catch { return; }
        if (message.type === "auth_required") {
          this.ws.send(JSON.stringify({ type: "auth", access_token: accessToken }));
          accessToken = null;
        } else if (message.type === "auth_ok") {
          this.connected = true;
          clearTimeout(timer);
          timer = null;
          resolve();
        } else if (message.type === "auth_invalid") {
          fail("native_ws_auth_invalid");
        } else if (message.type === "event") {
          this.eventHandlers.get(message.id)?.(message.event);
        } else if (message.type === "result" && this.pending.has(message.id)) {
          const pending = this.pending.get(message.id);
          this.pending.delete(message.id);
          clearTimeout(pending.timer);
          if (message.success) pending.resolve(message.result);
          else pending.reject(new Error("native_ws_command_failed"));
        }
      });
      this.ws.on("error", () => {
        this.transportError = true;
        this.connected = false;
        if (timer) fail("native_ws_transport");
      });
      this.ws.on("close", () => {
        if (!this.closedByUs) this.disconnectedCount += 1;
        this.connected = false;
        for (const pending of this.pending.values()) {
          clearTimeout(pending.timer);
          pending.reject(new Error("native_ws_disconnected"));
        }
        this.pending.clear();
        if (timer) fail("native_ws_closed_before_auth");
      });
    });
  }

  async command(payload, { eventHandler, timeoutMs = 12000 } = {}) {
    await this.ready;
    if (!this.snapshot().connected) throw new Error("native_ws_not_connected");
    const id = this.nextId++;
    if (eventHandler) this.eventHandlers.set(id, eventHandler);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("native_ws_command_timeout"));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.ws.send(JSON.stringify({ id, ...payload }), (error) => {
        if (!error) return;
        this.pending.delete(id);
        clearTimeout(timer);
        reject(new Error("native_ws_send_failed"));
      });
    });
  }

  async authenticateOwner(expectedOwnerId) {
    await this.ready;
    const user = await this.command({ type: "auth/current_user" });
    this.userId = typeof user?.id === "string" ? user.id : null;
    this.ownerMatches = user?.is_admin === true && this.userId === expectedOwnerId;
    if (!this.ownerMatches) throw new Error("native_ws_owner_mismatch");
  }

  async subscribeCallService() {
    const subscriptionId = this.nextId;
    await this.command({ type: "subscribe_events", event_type: "call_service" }, {
      eventHandler: (event) => this.events.push(serviceEventProjection(event, this.events.length)),
    });
    this.subscriptionId = subscriptionId;
    return subscriptionId;
  }

  async waitForEventAfter(offset, timeoutMs = 10000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (!this.snapshot().connected) throw new Error("native_ws_observer_disconnected");
      if (this.events.length > offset) return this.events[offset];
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    throw new Error("native_ws_event_timeout");
  }

  snapshot() {
    return {
      connection_identity: this.connectionIdentity,
      disconnected_count: this.disconnectedCount,
      same_connection: true,
      connected: this.connected && !this.transportError && this.ws.readyState === WebSocket.OPEN,
      owner_matches: this.ownerMatches,
    };
  }

  eventsSince(offset) {
    return this.events.slice(offset);
  }

  async close() {
    this.closedByUs = true;
    if (this.subscriptionId !== undefined && this.snapshot().connected) {
      try { await this.command({ type: "unsubscribe_events", subscription: this.subscriptionId }); } catch {}
    }
    try { this.ws.close(); } catch {}
    this.connected = false;
  }
}

function pathOnly(rawUrl) {
  try { return new URL(rawUrl).pathname; } catch { return null; }
}

async function attachDocumentJournal(page, privateTrace, pendingTurns) {
  const startedAt = Date.now();
  const journal = { events: [], mainFrameId: null, mainLoaderId: null, cdp: null };
  const record = (kind, data = {}) => {
    const item = { at: new Date().toISOString(), elapsed_ms: Date.now() - startedAt, kind, ...data };
    journal.events.push(item);
    privateTrace.document_events.push(item);
    return item;
  };
  const safeSignal = (value) => {
    if (!value || typeof value !== "object" || typeof value.kind !== "string") return;
    const allowed = ["kind", "document_marker", "pathname", "ready_state", "chat_instance_id",
      "has_previous", "persisted", "case_id", "accepted", "completed", "call_count",
      "latest_user_match", "assistant_present", "assistant_length", "final_busy", "status_error",
      "elapsed_ms", "trusted", "default_prevented_at_capture", "default_prevented_after_handlers",
      "target_tag", "type", "submitted_length", "error_class"];
    const data = {};
    for (const key of allowed) {
      if (key in value && (typeof value[key] === "string" || typeof value[key] === "number"
          || typeof value[key] === "boolean" || value[key] === null)) data[key] = value[key];
    }
    if (data.kind.startsWith("send_")) {
      privateTrace.send_callbacks.push({ at: new Date().toISOString(), ...data });
      if (data.kind === "send_completed" && typeof data.case_id === "string") {
        pendingTurns.get(data.case_id)?.resolve(data);
      } else if (data.kind === "send_failed" && typeof data.case_id === "string") {
        pendingTurns.get(data.case_id)?.reject(new Error(data.error_class || "chat_send_failed"));
      }
    } else if (data.kind === "submit_event") {
      privateTrace.submit_events.push({ at: new Date().toISOString(), ...data });
    }
    record("page_signal", data);
  };
  await page.exposeFunction("__hababQaRecord", safeSignal);

  page.on("framenavigated", (frame) => {
    let main = false;
    try { main = frame === page.mainFrame(); } catch {}
    record("puppeteer_frame_navigated", { main, pathname: pathOnly(frame.url()) });
  });
  page.on("domcontentloaded", () => record("puppeteer_domcontentloaded"));
  page.on("load", () => record("puppeteer_load"));
  page.on("pageerror", (error) => record("page_error", { error_class: safeErrorClass(error) }));
  page.on("requestfailed", (request) => record("request_failed", { pathname: pathOnly(request.url()) }));
  page.on("response", (response) => {
    if (response.status() >= 400) record("http_error", { pathname: pathOnly(response.url()), status: response.status() });
  });

  const cdp = await page.target().createCDPSession();
  journal.cdp = cdp;
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Network.enable");
  const frameTree = await cdp.send("Page.getFrameTree");
  journal.mainFrameId = frameTree.frameTree.frame.id;
  journal.mainLoaderId = frameTree.frameTree.frame.loaderId || null;
  cdp.on("Network.requestWillBeSent", (event) => {
    if (event.type !== "Document" || (journal.mainFrameId && event.frameId !== journal.mainFrameId)) return;
    const callFrames = event.initiator?.stack?.callFrames || [];
    record("cdp_document_request", {
      method: event.request?.method || null,
      pathname: pathOnly(event.request?.url),
      loader_id: event.loaderId || null,
      initiator_type: event.initiator?.type || null,
      initiator_functions: callFrames.slice(0, 4).map((frame) => frame.functionName || "(anonymous)"),
      initiator_paths: callFrames.slice(0, 4).map((frame) => pathOnly(frame.url)).filter(Boolean),
    });
  });
  cdp.on("Page.frameRequestedNavigation", (event) => record("cdp_frame_requested_navigation", {
    main: event.frameId === journal.mainFrameId, pathname: pathOnly(event.url), reason: event.reason || null,
  }));
  cdp.on("Page.frameScheduledNavigation", (event) => record("cdp_frame_scheduled_navigation", {
    pathname: pathOnly(event.url), reason: event.reason || null,
  }));
  cdp.on("Page.frameNavigated", (event) => {
    const frame = event.frame || {};
    const main = !frame.parentId;
    if (main) {
      journal.mainFrameId = frame.id || journal.mainFrameId;
      journal.mainLoaderId = frame.loaderId || journal.mainLoaderId;
    }
    record("cdp_frame_navigated", { main, pathname: pathOnly(frame.url), loader_id: frame.loaderId || null });
  });
  cdp.on("Page.navigatedWithinDocument", (event) => record("cdp_navigated_within_document", {
    pathname: pathOnly(event.url), navigation_type: event.navigationType || null,
  }));
  cdp.on("Runtime.executionContextCreated", (event) => {
    if (event.context?.auxData?.isDefault) record("js_default_context_created", {
      frame_id: event.context.auxData.frameId || null, context_id: event.context.id,
    });
  });
  cdp.on("Runtime.executionContextDestroyed", (event) => record("js_context_destroyed", {
    execution_context_id: event.executionContextId,
  }));
  cdp.on("Runtime.executionContextsCleared", () => record("js_contexts_cleared"));

  const markerSeed = crypto.randomBytes(12).toString("hex");
  await page.evaluateOnNewDocument((seed) => {
    const marker = `${seed}-${crypto.randomUUID()}`;
    window.__hababQaDocumentMarker = marker;
    const emit = (data) => {
      try {
        const signal = { ...data, document_marker: marker, pathname: location.pathname };
        const pending = window.__hababQaRecord?.(signal);
        if (pending && typeof pending.catch === "function") pending.catch(() => {});
      } catch {}
    };
    emit({ kind: "document_created", ready_state: document.readyState });
    window.addEventListener("beforeunload", () => emit({ kind: "beforeunload" }), { once: true });
    window.addEventListener("pagehide", (event) => emit({ kind: "pagehide", persisted: event.persisted }), { once: true });
    document.addEventListener("DOMContentLoaded", () => {
      emit({ kind: "document_domcontentloaded", ready_state: document.readyState });
      let previous = null;
      let nextId = 0;
      const observeChat = () => {
        const current = document.querySelector("habab-setup-chat");
        if (current === previous) return;
        previous = current;
        if (!current) {
          window.__hababQaCurrentChatInstanceId = null;
          emit({ kind: "chat_host_detached" });
          return;
        }
        const instanceId = ++nextId;
        current.__hababQaChatInstanceId = instanceId;
        window.__hababQaCurrentChatInstanceId = instanceId;
        emit({ kind: "chat_host_attached", chat_instance_id: instanceId });
      };
      const observer = new MutationObserver(observeChat);
      observer.observe(document.documentElement, { childList: true, subtree: true });
      observeChat();
      window.__hababQaChatObserver = observer;
    }, { once: true });
  }, markerSeed);
  return journal;
}

function sameDeviceInventoryCount(before, after) {
  return Number.isInteger(before) && before >= 0 && before === after;
}

function isReadyToSubmit(snapshot) {
  return snapshot?.chat_present === true
    && snapshot.chat_visible === true && snapshot.chat_not_inert === true
    && snapshot.owner_admin === true && snapshot.owner_matches === true
    && snapshot.hook_available === true && snapshot.session_ready === true
    && snapshot.busy === false && snapshot.running === false
    && snapshot.diagnostics_active === false
    && snapshot.connection_connected === true && snapshot.connection_same === true
    && snapshot.connection_disconnected_count === 0
    && snapshot.document_visible === true && snapshot.dialog_open === true
    && snapshot.textarea_visible === true && snapshot.textarea_not_inert === true
    && snapshot.textarea_enabled === true && snapshot.textarea_empty === true
    && snapshot.send_visible === true && snapshot.send_not_inert === true
    && snapshot.send_enabled === true;
}

function safeErrorClass(error) {
  const name = error instanceof Error ? error.name : "UnknownError";
  return /^[A-Za-z][A-Za-z0-9_]{0,39}$/.test(name) ? name : "OtherError";
}

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

function writePublicReport(file, value, reserve = false) {
  try {
    fs.writeFileSync(file, JSON.stringify(value, null, 2) + "\n",
      reserve ? { flag: "wx", mode: 0o644 } : { mode: 0o644 });
  } catch (error) {
    if (reserve && error?.code === "EEXIST") throw new Error("attempt_output_exists");
    throw error;
  }
}

function preserveFirstFailure(authFile) {
  const privateDir = path.dirname(path.resolve(authFile));
  const sourcePublic = FIRST_PUBLIC_OUTPUT;
  const sourcePrivate = path.join(privateDir, FIRST_PRIVATE_TRACE_NAME);
  const archivePublic = path.join(__dirname, "service-call-e2e-20261024-attempt-1.json");
  const archivePrivate = path.join(privateDir, "service-call-e2e-20261024-attempt-1-private.json");
  const sourcesExist = fs.existsSync(sourcePublic) && fs.existsSync(sourcePrivate);
  if (!sourcesExist) return;
  const archivePublicExists = fs.existsSync(archivePublic);
  const archivePrivateExists = fs.existsSync(archivePrivate);
  if (archivePublicExists && archivePrivateExists) return;
  if (archivePublicExists !== archivePrivateExists) throw new Error("prior_attempt_archive_incomplete");
  fs.copyFileSync(sourcePublic, archivePublic, fs.constants.COPYFILE_EXCL);
  fs.copyFileSync(sourcePrivate, archivePrivate, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(archivePrivate, 0o600);
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
    verdict: options.runLive ? "IN_PROGRESS" : "PREFLIGHT_PASS",
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
  if (fs.existsSync(privateTracePath) || fs.existsSync(OUTPUT)) throw new Error("attempt_output_exists");
  preserveFirstFailure(options["auth-file"]);
  const privateTrace = {
    runtime: runtime.identity,
    attempt: ATTEMPT_LABEL,
    helper_name: FIXTURE_NAME,
    prompts: [],
    replies: [],
    call_service_events: [],
    service_observer_events: [],
    document_events: [],
    send_callbacks: [],
    submit_events: [],
    helper_id: null,
    entity_id: null,
  };
  let nativeObserver;
  let documentJournal;
  let documentBaseline;
  let currentOwnerUserId;
  let currentSessionId;
  const pendingTurns = new Map();
  let fixtureCreateAttempted = false;
  let fixtureCreated = false;
  let helperId;
  let entityId;
  let priorHelperIds = new Set();
  let chatTurnMayBeActive = false;
  let currentCaseName = null;
  let currentPrompt = null;
  let currentCaseId = null;
  let currentEventOffset = 0;
  let currentDocumentEventOffset = 0;
  let currentObserverStart = null;
  let observerEventBoundary = null;
  let currentStage = "browser_startup";
  let setupError;
  let safeToDeleteFixture = true;
  const persistPrivate = () => privateWrite(privateTracePath, privateTrace);
  let outputReserved = false;
  const savePublic = (reserve = false) => {
    if (!reserve && !outputReserved) throw new Error("attempt_output_not_reserved");
    writePublicReport(OUTPUT, report, reserve);
    if (reserve) outputReserved = true;
  };
  report.attempt = ATTEMPT_LABEL;
  savePublic(true);

  async function cleanupWhileBrowserOpen(page) {
    currentStage = "cleanup";
    if (nativeObserver && observerEventBoundary !== null
        && nativeObserver.events.length !== observerEventBoundary
        && report.verdict === "ACTIONS_PASS_CLEANUP_PENDING_MANUAL_REVIEW") {
      report.verdict = "FAIL";
      report.failure_code = "unexpected_service_event_after_cases";
      privateTrace.unexpected_service_events_after_cases = nativeObserver.events.slice(observerEventBoundary);
      privateTrace.service_observer_events = nativeObserver.events;
      persistPrivate();
    }
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
      let activity = nativeObserver && currentSessionId
        ? await readBackendDiagnostics(nativeObserver, currentSessionId)
        : { active: null, error: "diagnostics_unavailable" };
      if (activity.active === true) {
        try {
          const stopResult = await page.evaluate(async () => {
            const chat = document.querySelector("habab-setup-chat");
            if (typeof chat?.stop !== "function") return "stop_unavailable";
            let timer;
            try {
              return await Promise.race([
                chat.stop().then(() => "stop_requested"),
                new Promise((resolve) => {
                  timer = setTimeout(() => resolve("stop_timeout"), 5000);
                }),
              ]);
            } catch {
              return "stop_failed";
            } finally {
              clearTimeout(timer);
            }
          });
          if (stopResult === "stop_requested") {
            try {
              await page.waitForFunction(() => !document.querySelector("habab-setup-chat")?.busy,
                { timeout: 20000, polling: 100 });
            } catch {
              // Backend diagnostics below still decide whether deletion is safe.
            }
          }
        } catch {
          report.active_turn_stop_attempted = false;
        }
        activity = nativeObserver && currentSessionId
          ? await readBackendDiagnostics(nativeObserver, currentSessionId)
          : { active: null, error: "diagnostics_unavailable" };
      }
      if (shouldDeferFixtureCleanup(chatTurnMayBeActive, activity.active)) {
        safeToDeleteFixture = false;
        report.cleanup.backend_idle_confirmed = false;
        report.cleanup.backend_status = activity.active === true ? "active" : "unavailable";
        report.verdict = "FAIL_CLEANUP";
        privateTrace.cleanup_deferred_backend_state = activity.active;
        privateTrace.cleanup_deferred_backend_error = activity.error;
        persistPrivate();
      } else {
        chatTurnMayBeActive = false;
        report.cleanup.backend_idle_confirmed = true;
        report.cleanup.backend_status = "idle";
      }
      if (!safeToDeleteFixture) {
        report.active_turn_stopped_before_cleanup = false;
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
          else if (report.verdict === "ACTIONS_PASS_CLEANUP_PENDING_MANUAL_REVIEW") {
            report.verdict = "PASS_PENDING_MANUAL_REVIEW";
          }
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

  async function captureFailureSnapshot(page, caseName, prompt, eventOffset, stage, errorClass) {
    let ui;
    try {
      ui = await page.evaluate(({ expectedText }) => {
        const documentState = {
          document_ready_state: document.readyState,
          document_marker: window.__hababQaDocumentMarker || null,
          document_pathname: window.location.pathname,
          document_visible: document.visibilityState === "visible",
        };
        const chat = document.querySelector("habab-setup-chat");
        if (!chat) return { ...documentState, chat_present: false };
        const messages = Array.isArray(chat.messages) ? chat.messages.slice(-2) : [];
        return {
          ...documentState,
          chat_present: true,
          chat_instance_id: chat.__hababQaChatInstanceId ?? null,
          same_host_as_baseline: window.__hababQaBaselineChat === chat,
          busy: Boolean(chat.busy),
          running: Boolean(chat.running),
          status_error: Boolean(chat.shadowRoot.querySelector("#status")?.classList.contains("error")),
          dialog_open: Boolean(chat.shadowRoot.querySelector("dialog")?.open),
          session_ready: Boolean(chat.sessionId),
          owner_admin: chat.hass?.user?.is_admin === true,
          owner_user_id: chat.hass?.user?.id || null,
          session_id: chat.sessionId || null,
          hook_available: window.hababAssistant?.available === true,
          turn_probe: chat.__hababQaAttempt5Probe || null,
          latest_messages: messages.map((message) => ({ role: message.role, text: String(message.text || "") })),
          exact_prompt_at_tail: messages.length === 2 && messages[0]?.role === "user"
            && messages[0].text === expectedText,
          assistant_at_tail: messages.length === 2 && messages[1]?.role === "assistant",
        };
      }, { expectedText: prompt });
    } catch (error) {
      ui = { snapshot_error: error instanceof Error ? error.name : "UnknownError" };
    }
    let frame = null;
    try { frame = (await documentJournal?.cdp.send("Page.getFrameTree"))?.frameTree?.frame || null; } catch {}
    const endDocument = ui.chat_present ? {
      document_marker: ui.document_marker,
      main_frame_id: frame?.id || null,
      main_loader_id: frame?.loaderId || null,
      pathname: ui.document_pathname,
      chat_instance_id: ui.chat_instance_id,
      same_host_as_baseline: ui.same_host_as_baseline,
      owner_user_id: ui.owner_user_id,
      session_id: ui.session_id,
    } : null;
    const sameDocument = Boolean(documentBaseline && endDocument && hasSameDocument(documentBaseline, endDocument));
    const observerEnd = nativeObserver?.snapshot() || null;
    const rawServiceEvents = nativeObserver?.eventsSince(eventOffset) || null;
    const observedServiceEvents = currentObserverStart && observerEnd && rawServiceEvents
      ? observedEventsIfContinuous(currentObserverStart, observerEnd, rawServiceEvents) : null;
    const diagnostics = nativeObserver && currentSessionId
      ? await readBackendDiagnostics(nativeObserver, currentSessionId)
      : { active: null, error: "diagnostics_unavailable" };
    const callbackEvents = privateTrace.send_callbacks.filter((item) => item.case_id === currentCaseId);
    const callbackCompleted = Boolean(documentBaseline && hasCompletedSendCallbacks(callbackEvents, currentCaseId, documentBaseline));
    let fixtureState = null;
    let fixtureStateReadError = false;
    if (entityId) {
      try {
        fixtureState = await readState(runtime, entityId);
      } catch {
        fixtureStateReadError = true;
      }
    }
    privateTrace.failure_snapshot = {
      case_name: caseName,
      stage,
      error_class: errorClass,
      prompt,
      captured_at: new Date().toISOString(),
      completed_probe: callbackEvents,
      ui,
      document_baseline: documentBaseline,
      document_end: endDocument,
      same_document: sameDocument,
      document_events_since_case: documentJournal?.events.slice(currentDocumentEventOffset) || [],
      observer_start: currentObserverStart,
      observer_end: observerEnd,
      observer_continuous: Boolean(observedServiceEvents),
      service_events: rawServiceEvents,
      all_service_events: nativeObserver?.events || null,
      backend_diagnostics: diagnostics,
      fixture_state: fixtureState,
      fixture_state_read_error: fixtureStateReadError,
    };
    report.failure_snapshot = {
      case_name: caseName,
      stage,
      error_class: errorClass,
      ui_snapshot_captured: !ui.snapshot_error,
      document_ready_state: ui.document_ready_state ?? null,
      document_marker_same: sameDocument,
      document_pathname: ui.document_pathname ?? null,
      document_visible: ui.document_visible ?? null,
      chat_present: ui.chat_present ?? false,
      same_chat_instance: ui.same_host_as_baseline ?? false,
      session_ready: ui.session_ready ?? null,
      owner_admin: ui.owner_admin ?? null,
      hook_available: ui.hook_available ?? null,
      busy: ui.busy ?? null,
      status_error: ui.status_error ?? null,
      send_callback_completed: callbackCompleted,
      probe_present: ui.turn_probe !== undefined && ui.turn_probe !== null,
      probe_completed: ui.turn_probe?.completed ?? null,
      exact_prompt_at_tail: ui.exact_prompt_at_tail ?? null,
      assistant_at_tail: ui.assistant_at_tail ?? null,
      backend_active: diagnostics.active,
      observer_identity: observerEnd?.connection_identity ?? null,
      observer_connected: observerEnd?.connected ?? null,
      observer_owner_matches: observerEnd?.owner_matches ?? null,
      observer_disconnect_count: observerEnd?.disconnected_count ?? null,
      service_event_count: Array.isArray(observedServiceEvents) ? observedServiceEvents.length : null,
      fixture_readback_state: fixtureState?.state ?? null,
      fixture_readback_error: fixtureStateReadError,
    };
    persistPrivate();
    savePublic();
  }

  try {
    currentStage = "browser_startup";
    await withOwnedChromium(puppeteer, {
      taskTimeoutMs: 900000,
      args: ["--disable-dev-shm-usage"],
      defaultViewport: { width: 1280, height: 800 },
    }, async ({ page }) => {
      try {
      documentJournal = await attachDocumentJournal(page, privateTrace, pendingTurns);
      await page.evaluateOnNewDocument((auth) => {
        localStorage.setItem("hassTokens", JSON.stringify({
          ...auth,
          hassUrl: "https://habab-demo.wpmix.net",
          clientId: auth.client_id,
          expires: Date.now() + (auth.expires_in || 3600) * 1000,
        }));
        localStorage.setItem("selectedLanguage", JSON.stringify("ru"));
      }, runtime.auth);
      currentStage = "owner_test_route";
      await page.goto(runtime.base, { waitUntil: "networkidle2", timeout: 60000 });
      const ownerTestPathname = await page.evaluate(() => window.location.pathname);
      report.owner_test_pathname = ownerTestPathname;
      privateTrace.owner_test_pathname = ownerTestPathname;
      currentStage = "native_route";
      await page.goto(`${runtime.origin}/home/overview`, { waitUntil: "networkidle2", timeout: 60000 });
      const browserPathname = await page.evaluate(() => window.location.pathname);
      report.browser_pathname = browserPathname;
      privateTrace.browser_pathname = browserPathname;
      assert.equal(browserPathname, "/home/overview", "native_chat_route_mismatch");
      currentStage = "chat_ready";
      await page.waitForFunction(() => {
        const chat = document.querySelector("habab-setup-chat");
        return !!chat?.hass?.user?.is_admin && !!chat.hass.callWS && !!window.hababAssistant?.available;
      }, { timeout: 60000 });
      await page.waitForFunction(() => {
        const chat = document.querySelector("habab-setup-chat");
        return !!chat?.sessionId && !chat.busy;
      }, { timeout: 90000 });
      const initialChat = await page.evaluate(() => {
        const chat = document.querySelector("habab-setup-chat");
        const dialog = chat.shadowRoot.querySelector("dialog");
        const launcher = chat.shadowRoot.querySelector(".launcher");
        const rect = launcher?.getBoundingClientRect();
        return {
          dialog_open: dialog.open,
          draft_empty: !chat.shadowRoot.querySelector("textarea").value.trim(),
          history_count: chat.messages.length,
          launcher_visible: Boolean(rect?.width && rect?.height),
          launcher_x: rect ? rect.left + rect.width / 2 : null,
          launcher_y: rect ? rect.top + rect.height / 2 : null,
        };
      });
      if (!initialChat.dialog_open) {
        assert.ok(initialChat.launcher_visible, "assistant_launcher_not_visible");
        await page.mouse.click(initialChat.launcher_x, initialChat.launcher_y);
        await page.waitForFunction(() => document.querySelector("habab-setup-chat")
          ?.shadowRoot?.querySelector("dialog")?.open === true, { timeout: 5000 });
      }
      const ready = await page.evaluate(() => {
        const chat = document.querySelector("habab-setup-chat");
        return { dialog_open: chat.shadowRoot.querySelector("dialog").open,
          draft_empty: !chat.shadowRoot.querySelector("textarea").value.trim(),
          history_count: chat.messages.length };
      });
      assert.equal(ready.dialog_open, true, "assistant_dialog_not_open");
      assert.equal(ready.draft_empty, true, "existing_draft_present");
      report.preexisting_history_count = ready.history_count;

      currentStage = "fixture_setup";
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
      assert.ok(entity?.entity_id?.startsWith("input_boolean.habab_qa_service_call_fixture_20261024"), "fixture_entity_id_mismatch");
      entityId = entity.entity_id;
      privateTrace.entity_id = entityId;
      persistPrivate();
      const initial = await readState(runtime, entityId);
      assert.equal(initial?.friendly_name, FIXTURE_NAME, "fixture_name_mismatch");
      assert.equal(initial.state, "off", "fixture_initial_state_not_off");
      const ownerUserId = await page.evaluate(() => document.querySelector("habab-setup-chat").hass.user?.id);
      assert.ok(typeof ownerUserId === "string" && ownerUserId, "authenticated_user_id_missing");
      currentOwnerUserId = ownerUserId;
      currentSessionId = await page.evaluate(() => document.querySelector("habab-setup-chat")?.sessionId || null);
      assert.ok(typeof currentSessionId === "string" && currentSessionId, "authenticated_session_id_missing");
      privateTrace.owner_user_id = ownerUserId;
      privateTrace.session_id = currentSessionId;

      currentStage = "observer_setup";
      nativeObserver = new NativeHaServiceObserver(runtime.auth.access_token);
      await nativeObserver.authenticateOwner(ownerUserId);
      await nativeObserver.subscribeCallService();
      const observerReady = nativeObserver.snapshot();
      assert.ok(hasContinuousObserver(observerReady, observerReady), "native_service_observer_unavailable");
      report.service_observer = { authenticated_owner_match: true, subscribed: true,
        connection_identity: observerReady.connection_identity, connected: true };

      // Calibrate the event observer through native HA helper/service APIs.
      // These two direct calls are apparatus only, never counted as chat proof.
      async function calibrateNativeService(service, expectedState) {
        currentStage = "observer_calibration";
        const observerStart = nativeObserver.snapshot();
        const offset = nativeObserver.events.length;
        await page.evaluate(({ service, fixtureEntityId }) => document.querySelector("habab-setup-chat").hass.callService(
          "input_boolean", service, { entity_id: fixtureEntityId }), { service, fixtureEntityId: entityId });
        const event = await nativeObserver.waitForEventAfter(offset, 10000);
        const state = await readState(runtime, entityId);
        const observerEnd = nativeObserver.snapshot();
        assert.ok(hasContinuousObserver(observerStart, observerEnd), "calibration_observer_continuity_lost");
        const allEvents = observedEventsIfContinuous(observerStart, observerEnd, nativeObserver.eventsSince(offset));
        assert.ok(Array.isArray(allEvents), "calibration_observer_continuity_lost");
        assert.equal(allEvents.length, 1, "calibration_unexpected_service_event_count");
        assert.equal(event.domain, "input_boolean", "calibration_wrong_domain");
        assert.equal(event.service, service, "calibration_wrong_service");
        assert.ok(serviceEventTargetsOnlyEntity(event, entityId), "calibration_wrong_target");
        assert.equal(event.context_user_id, ownerUserId, "calibration_context_mismatch");
        assert.equal(state?.state, expectedState, "calibration_readback_mismatch");
        privateTrace.calibration = privateTrace.calibration || [];
        privateTrace.calibration.push({ service, event, readback: state?.state,
          connection_identity: observerStart.connection_identity,
          disconnected_count_start: observerStart.disconnected_count,
          disconnected_count_end: observerEnd.disconnected_count });
        report.calibration = { observer_acknowledged: true, calls_observed: privateTrace.calibration.length,
          target_matched: true, context_matched: true, state_restored_off: expectedState === "off",
          connection_identity: observerStart.connection_identity,
          uninterrupted: true };
        persistPrivate();
        savePublic();
      }
      await calibrateNativeService("turn_on", "on");
      await calibrateNativeService("turn_off", "off");
      observerEventBoundary = nativeObserver.events.length;

      const baselineUi = await page.evaluate(() => {
        const chat = document.querySelector("habab-setup-chat");
        if (!chat) return null;
        window.__hababQaBaselineChat = chat;
        return {
          document_marker: window.__hababQaDocumentMarker || null,
          pathname: location.pathname,
          chat_instance_id: chat.__hababQaChatInstanceId ?? null,
          same_host_as_baseline: window.__hababQaBaselineChat === chat,
          host_connected: chat.isConnected,
          owner_user_id: chat.hass?.user?.id || null,
          owner_admin: chat.hass?.user?.is_admin === true,
          session_id: chat.sessionId || null,
        };
      });
      const baselineFrame = (await documentJournal.cdp.send("Page.getFrameTree")).frameTree.frame;
      documentBaseline = {
        ...baselineUi,
        main_frame_id: baselineFrame.id,
        main_loader_id: baselineFrame.loaderId || null,
      };
      assert.ok(documentBaseline.document_marker, "document_marker_unavailable");
      assert.equal(documentBaseline.pathname, "/home/overview", "baseline_route_mismatch");
      assert.ok(Number.isInteger(documentBaseline.chat_instance_id), "chat_instance_id_unavailable");
      assert.equal(documentBaseline.same_host_as_baseline, true, "chat_baseline_reference_failed");
      assert.equal(documentBaseline.host_connected, true, "chat_host_not_connected");
      assert.equal(documentBaseline.owner_user_id, ownerUserId, "baseline_owner_mismatch");
      assert.equal(documentBaseline.owner_admin, true, "baseline_owner_not_admin");
      assert.equal(documentBaseline.session_id, currentSessionId, "baseline_session_mismatch");
      assert.ok(documentBaseline.main_frame_id && documentBaseline.main_loader_id, "main_document_identity_unavailable");
      currentDocumentEventOffset = documentJournal.events.length;
      privateTrace.document_baseline = documentBaseline;
      report.document_monitoring = { marker_present: true, loader_present: true,
        chat_instance_present: true, path: "/home/overview" };
      persistPrivate();
      savePublic();

      async function sendTurn(name, prompt, expectedState, expectedService, options = {}) {
        currentCaseName = name;
        currentCaseId = `${ATTEMPT_LABEL}:${name}`;
        currentPrompt = prompt;
        currentStage = options.verifyDeviceInventory ? "device_inventory_read_before" : "submit_preconditions";
        let deviceRegistryCountBefore = null;
        let deviceRegistryCountAfter = null;
        if (options.verifyDeviceInventory) {
          deviceRegistryCountBefore = await page.evaluate(async () => {
            const devices = await document.querySelector("habab-setup-chat").hass.callWS({
              type: "config/device_registry/list",
            });
            return Array.isArray(devices) ? devices.length : null;
          });
          assert.ok(Number.isInteger(deviceRegistryCountBefore), "device_registry_read_before_failed");
          privateTrace.device_inventory = { device_registry_count_before: deviceRegistryCountBefore };
          persistPrivate();
        }

        const observerStart = nativeObserver.snapshot();
        assert.ok(hasContinuousObserver(observerStart, observerStart), `${name}_observer_not_ready`);
        assert.equal(nativeObserver.events.length, observerEventBoundary, `${name}_unexpected_service_event_between_cases`);
        currentObserverStart = observerStart;
        currentEventOffset = nativeObserver.events.length;
        currentDocumentEventOffset = documentJournal.events.length;
        const diagnosticsStart = await readBackendDiagnostics(nativeObserver, currentSessionId);
        assert.equal(diagnosticsStart.active, false, `${name}_backend_not_idle_before_submit`);
        assert.ok(hasContinuousObserver(observerStart, nativeObserver.snapshot()),
          `${name}_observer_lost_during_precheck`);

        currentStage = "submit_preconditions";
        const startUi = await page.evaluate(async ({ ownerUserId, sessionId }) => {
          const chat = document.querySelector("habab-setup-chat");
          const input = chat?.shadowRoot?.querySelector("textarea");
          const send = chat?.shadowRoot?.querySelector(".send");
          const dialog = chat?.shadowRoot?.querySelector("dialog");
          const visible = (element, pointer = true) => {
            if (!element?.isConnected) return { shown: false, notInert: false, x: null, y: null };
            let notInert = true;
            for (let node = element; node;) {
              if (node.inert || node.hasAttribute?.("inert") || node.getAttribute?.("aria-hidden") === "true") notInert = false;
              const root = node.getRootNode?.();
              node = node.parentElement || (root instanceof ShadowRoot ? root.host : null);
            }
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            const shown = rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0
              && rect.top < innerHeight && rect.left < innerWidth && style.display !== "none"
              && style.visibility !== "hidden" && (!pointer || style.pointerEvents !== "none");
            return { shown, notInert, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
          };
          const inputState = visible(input);
          const sendState = visible(send);
          return {
            document_marker: window.__hababQaDocumentMarker || null,
            pathname: location.pathname,
            chat_instance_id: chat?.__hababQaChatInstanceId ?? null,
            same_host_as_baseline: window.__hababQaBaselineChat === chat,
            host_connected: Boolean(chat?.isConnected),
            chat_present: Boolean(chat),
            chat_visible: visible(chat, false).shown,
            chat_not_inert: visible(chat, false).notInert,
            owner_admin: chat?.hass?.user?.is_admin === true,
            owner_user_id: chat?.hass?.user?.id || null,
            owner_matches: typeof ownerUserId === "string" && chat?.hass?.user?.id === ownerUserId
              && chat?.isCurrentOwnerContext?.() === true,
            hook_available: window.hababAssistant?.available === true,
            session_id: chat?.sessionId || null,
            session_ready: chat?.sessionId === sessionId,
            busy: Boolean(chat?.busy),
            running: Boolean(chat?.running),
            document_visible: document.visibilityState === "visible",
            dialog_open: dialog?.open === true,
            textarea_visible: inputState.shown,
            textarea_not_inert: inputState.notInert,
            textarea_enabled: input?.disabled === false,
            textarea_empty: typeof input?.value === "string" && input.value.trim() === "",
            send_visible: sendState.shown,
            send_not_inert: sendState.notInert,
            send_enabled: send?.disabled === false,
            input_x: inputState.x,
            input_y: inputState.y,
            send_x: sendState.x,
            send_y: sendState.y,
          };
        }, { ownerUserId: currentOwnerUserId, sessionId: currentSessionId });
        const frameStart = (await documentJournal.cdp.send("Page.getFrameTree")).frameTree.frame;
        const startDocument = {
          document_marker: startUi.document_marker,
          main_frame_id: frameStart.id,
          main_loader_id: frameStart.loaderId || null,
          pathname: startUi.pathname,
          chat_instance_id: startUi.chat_instance_id,
          same_host_as_baseline: startUi.same_host_as_baseline,
          owner_user_id: startUi.owner_user_id,
          session_id: startUi.session_id,
        };
        assert.ok(isReadyToSubmit({
          ...startUi,
          diagnostics_active: diagnosticsStart.active,
          connection_connected: observerStart.connected,
          connection_same: observerStart.same_connection,
          connection_disconnected_count: observerStart.disconnected_count,
        }), `${name}_submit_not_ready`);
        assert.ok(hasSameDocument(documentBaseline, startDocument), `${name}_document_changed_before_submit`);
        privateTrace.prompts.push({ name, text: prompt });
        privateTrace.turn_starts = privateTrace.turn_starts || [];
        privateTrace.turn_starts.push({ name, case_id: currentCaseId, document: startDocument,
          observer: observerStart, diagnostics: diagnosticsStart });
        persistPrivate();

        const pending = new Promise((resolve, reject) => {
          const timer = setTimeout(() => {
            pendingTurns.delete(currentCaseId);
            reject(new Error(`${name}_send_promise_timeout`));
          }, 210000);
          pendingTurns.set(currentCaseId, {
            resolve: (value) => { clearTimeout(timer); pendingTurns.delete(currentCaseId); resolve(value); },
            reject: (error) => { clearTimeout(timer); pendingTurns.delete(currentCaseId); reject(error); },
          });
        });
        chatTurnMayBeActive = true;
        currentStage = "chat_submit";
        const installed = await page.evaluate(({ expectedText, caseId, expectedOwnerId, expectedSessionId }) => {
          const chat = document.querySelector("habab-setup-chat");
          const input = chat?.shadowRoot?.querySelector("textarea");
          const send = chat?.shadowRoot?.querySelector(".send");
          const dialog = chat?.shadowRoot?.querySelector("dialog");
          if (!chat?.isConnected || window.__hababQaBaselineChat !== chat
              || !chat.isCurrentOwnerContext?.() || chat.hass?.user?.is_admin !== true
              || chat.hass?.user?.id !== expectedOwnerId || chat.sessionId !== expectedSessionId
              || !window.hababAssistant?.available || chat.busy || chat.running
              || !dialog?.open || input?.disabled || input?.value.trim() || send?.disabled) return null;
          const emit = (event) => {
            const operation = window.__hababQaRecord?.(event);
            if (operation && typeof operation.catch === "function") operation.catch(() => {});
          };
          if (!chat.__hababQaAttempt5OriginalSend) {
            chat.__hababQaAttempt5OriginalSend = chat.send;
            chat.send = async function (...args) {
              const probe = this.__hababQaAttempt5Probe;
              const submitted = typeof args[0] === "string" ? args[0].trim()
                : this.shadowRoot?.querySelector("textarea")?.value?.trim();
              if (!probe || submitted !== probe.expected_text) {
                return this.__hababQaAttempt5OriginalSend.apply(this, args);
              }
              const startedAt = Date.now();
              const beforeBusy = Boolean(this.busy);
              const beforeCount = this.messages.length;
              emit({ kind: "send_started", case_id: probe.case_id, document_marker: window.__hababQaDocumentMarker,
                chat_instance_id: this.__hababQaChatInstanceId, submitted_length: submitted.length });
              let operation;
              try {
                operation = this.__hababQaAttempt5OriginalSend.apply(this, args);
              } catch (error) {
                emit({ kind: "send_failed", case_id: probe.case_id, document_marker: window.__hababQaDocumentMarker,
                  chat_instance_id: this.__hababQaChatInstanceId,
                  error_class: error instanceof Error ? error.name : "UnknownError" });
                throw error;
              }
              const optimistic = this.messages.at(-1);
              probe.submitted_text = submitted;
              probe.accepted = !beforeBusy && this.busy === true && this.messages.length === beforeCount + 1
                && optimistic?.role === "user" && optimistic.text === submitted;
              probe.started_at = startedAt;
              emit({ kind: "send_accepted", case_id: probe.case_id, document_marker: window.__hababQaDocumentMarker,
                chat_instance_id: this.__hababQaChatInstanceId, accepted: probe.accepted });
              try {
                return await operation;
              } catch (error) {
                probe.error_class = error instanceof Error ? error.name : "UnknownError";
                throw error;
              } finally {
                probe.completed = true;
                probe.finished_at = Date.now();
                probe.elapsed_ms = probe.finished_at - startedAt;
                probe.final_busy = Boolean(this.busy);
                probe.status_error = Boolean(this.shadowRoot.querySelector("#status")?.classList.contains("error"));
                const latest = this.messages.slice(-2);
                probe.latest_user_match = latest.length === 2 && latest[0]?.role === "user"
                  && latest[0].text === probe.expected_text;
                probe.assistant_present = latest.length === 2 && latest[1]?.role === "assistant"
                  && typeof latest[1].text === "string" && Boolean(latest[1].text.trim());
                emit({ kind: "send_completed", case_id: probe.case_id, document_marker: window.__hababQaDocumentMarker,
                  chat_instance_id: this.__hababQaChatInstanceId, accepted: probe.accepted, completed: true,
                  final_busy: probe.final_busy, status_error: probe.status_error,
                  latest_user_match: probe.latest_user_match, assistant_present: probe.assistant_present,
                  assistant_length: latest.at(-1)?.role === "assistant" ? String(latest.at(-1).text || "").length : 0,
                  elapsed_ms: probe.elapsed_ms, error_class: probe.error_class || null });
              }
            };
          }
          const form = chat.shadowRoot?.querySelector("form");
          if (form && !form.__hababQaAttempt5SubmitListener) {
            form.__hababQaAttempt5SubmitListener = true;
            form.addEventListener("submit", (event) => {
              const activeProbe = chat.__hababQaAttempt5Probe;
              if (!activeProbe) return;
              const data = { kind: "submit_event", case_id: activeProbe.case_id,
                trusted: event.isTrusted, default_prevented_at_capture: event.defaultPrevented,
                submitted_length: input?.value?.length || 0 };
              emit(data);
              setTimeout(() => emit({ ...data, default_prevented_after_handlers: event.defaultPrevented }), 0);
            }, { capture: true });
          }
          if (chat.busy || send.disabled) return null;
          chat.__hababQaAttempt5Probe = { case_id: caseId, expected_text: expectedText,
            accepted: false, completed: false };
          return { x: input.getBoundingClientRect().left + input.getBoundingClientRect().width / 2,
            y: input.getBoundingClientRect().top + input.getBoundingClientRect().height / 2 };
        }, { expectedText: prompt, caseId: currentCaseId, expectedOwnerId: currentOwnerUserId,
          expectedSessionId: currentSessionId });
        assert.ok(installed, `${name}_submit_preconditions_changed`);
        await page.mouse.click(installed.x, installed.y);
        await page.keyboard.type(prompt);

        const sendPosition = await page.evaluate(async ({ expectedText, expectedOwnerId, expectedSessionId }) => {
          const chat = document.querySelector("habab-setup-chat");
          const input = chat?.shadowRoot?.querySelector("textarea");
          const send = chat?.shadowRoot?.querySelector(".send");
          const actionable = (element) => {
            if (!element?.isConnected || element.disabled) return null;
            for (let node = element; node;) {
              if (node.inert || node.hasAttribute?.("inert") || node.getAttribute?.("aria-hidden") === "true") return null;
              const root = node.getRootNode?.();
              node = node.parentElement || (root instanceof ShadowRoot ? root.host : null);
            }
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            if (rect.width <= 0 || rect.height <= 0 || rect.bottom <= 0 || rect.right <= 0
                || rect.top >= innerHeight || rect.left >= innerWidth || style.display === "none"
                || style.visibility === "hidden" || style.pointerEvents === "none") return null;
            return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2,
              left: rect.left, top: rect.top, width: rect.width, height: rect.height };
          };
          const valid = () => chat?.isConnected && window.__hababQaBaselineChat === chat
            && chat.isCurrentOwnerContext?.() && chat.hass?.user?.is_admin === true
            && chat.hass?.user?.id === expectedOwnerId && chat.sessionId === expectedSessionId
            && window.hababAssistant?.available && !chat.busy && !chat.running
            && document.visibilityState === "visible" && chat.shadowRoot.querySelector("dialog")?.open
            && input?.value === expectedText && input.disabled === false && send?.disabled === false
            && actionable(input) && actionable(send);
          if (!valid()) return null;
          const first = actionable(send);
          await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          if (!valid()) return null;
          const second = actionable(send);
          if (!first || !second || Math.abs(first.x - second.x) > 1 || Math.abs(first.y - second.y) > 1
              || Math.abs(first.width - second.width) > 1 || Math.abs(first.height - second.height) > 1) return null;
          const shadowHit = chat.shadowRoot.elementFromPoint(second.x, second.y);
          const documentHit = document.elementFromPoint(second.x, second.y);
          if (!(shadowHit === send || send.contains(shadowHit)) || documentHit !== chat) return null;
          return { x: second.x, y: second.y, hit_tag: shadowHit?.tagName || null,
            document_retarget_host: true, stable_geometry: true, input_matches: input.value === expectedText,
            document_marker: window.__hababQaDocumentMarker || null, pathname: location.pathname,
            chat_instance_id: chat.__hababQaChatInstanceId ?? null, owner_user_id: chat.hass?.user?.id || null,
            session_id: chat.sessionId || null };
        }, { expectedText: prompt, expectedOwnerId: currentOwnerUserId, expectedSessionId: currentSessionId });
        assert.ok(sendPosition, `${name}_send_button_not_actionable`);
        const beforeClickObserver = nativeObserver.snapshot();
        assert.ok(hasContinuousObserver(observerStart, beforeClickObserver), `${name}_observer_lost_before_click`);
        const beforeClickDiagnostics = await readBackendDiagnostics(nativeObserver, currentSessionId);
        assert.equal(beforeClickDiagnostics.active, false, `${name}_backend_not_idle_before_click`);
        assert.ok(hasContinuousObserver(observerStart, nativeObserver.snapshot()),
          `${name}_observer_lost_during_click_precheck`);
        const beforeClickDocument = {
          ...sendPosition,
          main_frame_id: frameStart.id,
          main_loader_id: frameStart.loaderId || null,
          same_host_as_baseline: true,
        };
        const beforeClickFrame = (await documentJournal.cdp.send("Page.getFrameTree")).frameTree.frame;
        beforeClickDocument.main_frame_id = beforeClickFrame.id;
        beforeClickDocument.main_loader_id = beforeClickFrame.loaderId || null;
        assert.ok(hasSameDocument(documentBaseline, beforeClickDocument), `${name}_document_changed_before_click`);
        assert.ok(hasNoDocumentNavigation(documentJournal.events.slice(currentDocumentEventOffset)),
          `${name}_navigation_detected_before_click`);
        privateTrace.submit_events.push({ kind: "target_preflight", name, ...sendPosition,
          trusted_pointer_click: true });
        persistPrivate();
        await page.mouse.click(sendPosition.x, sendPosition.y, { delay: 35 });

        currentStage = "chat_completion";
        const callback = await pending;
        const documentEventsForTurn = documentJournal.events.slice(currentDocumentEventOffset);
        assert.ok(hasNoDocumentNavigation(documentEventsForTurn), `${name}_navigation_detected_during_turn`);
        assert.equal(callback.accepted, true, `${name}_submission_not_accepted`);
        assert.equal(callback.completed, true, `${name}_send_promise_not_settled`);
        assert.equal(callback.final_busy, false, `${name}_send_promise_ended_busy`);
        const callbackEvents = privateTrace.send_callbacks.filter((event) => event.case_id === currentCaseId);
        assert.ok(hasCompletedSendCallbacks(callbackEvents, currentCaseId, documentBaseline), `${name}_send_callback_invalid`);

        const turn = await page.evaluate((expectedText, expectedCaseId) => {
          const chat = document.querySelector("habab-setup-chat");
          if (!chat) return { chat_present: false, messages: [] };
          const messages = Array.isArray(chat.messages) ? chat.messages.slice(-2)
            .map((message) => ({ role: message.role, text: String(message.text || "") })) : [];
          return {
            chat_present: true,
            document_marker: window.__hababQaDocumentMarker || null,
            pathname: location.pathname,
            chat_instance_id: chat.__hababQaChatInstanceId ?? null,
            same_host_as_baseline: window.__hababQaBaselineChat === chat,
            owner_user_id: chat.hass?.user?.id || null,
            owner_admin: chat.hass?.user?.is_admin === true,
            owner_context_current: chat?.isCurrentOwnerContext?.() === true,
            hook_available: window.hababAssistant?.available === true,
            session_id: chat.sessionId || null,
            busy: Boolean(chat.busy), running: Boolean(chat.running),
            status_error: Boolean(chat.shadowRoot.querySelector("#status")?.classList.contains("error")),
            messages,
            exact_prompt_at_tail: messages.length === 2 && messages[0]?.role === "user" && messages[0].text === expectedText,
            assistant_at_tail: messages.length === 2 && messages[1]?.role === "assistant"
              && typeof messages[1].text === "string" && Boolean(messages[1].text.trim()),
            current_node_has_same_probe: chat.__hababQaAttempt5Probe?.case_id === expectedCaseId,
          };
        }, prompt, currentCaseId);
        const frameEnd = (await documentJournal.cdp.send("Page.getFrameTree")).frameTree.frame;
        const endDocument = {
          document_marker: turn.document_marker,
          main_frame_id: frameEnd.id,
          main_loader_id: frameEnd.loaderId || null,
          pathname: turn.pathname,
          chat_instance_id: turn.chat_instance_id,
          same_host_as_baseline: turn.same_host_as_baseline,
          owner_user_id: turn.owner_user_id,
          session_id: turn.session_id,
        };
        const sameDocument = hasSameDocument(documentBaseline, endDocument);
        assert.ok(sameDocument, `${name}_document_or_chat_changed`);
        assert.equal(turn.busy, false, `${name}_current_ui_busy`);
        assert.equal(turn.status_error, false, `${name}_current_ui_error`);
        assert.equal(turn.owner_admin, true, `${name}_owner_not_admin_after_turn`);
        assert.equal(turn.owner_context_current, true, `${name}_owner_context_changed`);
        assert.equal(turn.hook_available, true, `${name}_chat_hook_unavailable_after_turn`);
        assert.equal(turn.exact_prompt_at_tail, true, `${name}_exact_prompt_not_at_tail`);
        assert.equal(turn.assistant_at_tail, true, `${name}_assistant_reply_missing`);
        const completedProbe = {
          expected_text: prompt, submitted_text: prompt, accepted: callback.accepted,
          completed: callback.completed, final_busy: callback.final_busy,
          status_error: callback.status_error === true,
        };
        assert.ok(hasAcceptedTurn(turn.messages, prompt, completedProbe,
          { busy: turn.busy, status_error: turn.status_error }), `${name}_turn_not_correlated`);

        const diagnostics = await readBackendDiagnostics(nativeObserver, currentSessionId);
        const observerEnd = nativeObserver.snapshot();
        const observedEvents = observedEventsIfContinuous(observerStart, observerEnd,
          nativeObserver.eventsSince(currentEventOffset));
        assert.ok(Array.isArray(observedEvents), `${name}_observer_continuity_lost`);
        const events = observedEvents;
        currentStage = "state_readback";
        let state = await readState(runtime, entityId);
        const deadline = Date.now() + 20000;
        while (expectedState && state?.state !== expectedState && Date.now() < deadline) {
          await new Promise((resolve) => setTimeout(resolve, 500));
          state = await readState(runtime, entityId);
        }
        assert.equal(state?.friendly_name, FIXTURE_NAME, `${name}_fixture_missing`);
        assert.equal(state.state, expectedState, `${name}_readback_mismatch`);
        const observerAfterReadback = nativeObserver.snapshot();
        const allEvents = observedEventsIfContinuous(observerStart, observerAfterReadback,
          nativeObserver.eventsSince(currentEventOffset));
        assert.ok(Array.isArray(allEvents), `${name}_observer_disconnected_during_readback`);
        assert.equal(allEvents.length, events.length, `${name}_service_events_changed_after_snapshot`);
        observerEventBoundary = nativeObserver.events.length;

        if (options.verifyDeviceInventory) {
          deviceRegistryCountAfter = await page.evaluate(async () => {
            const devices = await document.querySelector("habab-setup-chat").hass.callWS({
              type: "config/device_registry/list",
            });
            return Array.isArray(devices) ? devices.length : null;
          });
          privateTrace.device_inventory.device_registry_count_after = deviceRegistryCountAfter;
          persistPrivate();
          assert.ok(sameDeviceInventoryCount(deviceRegistryCountBefore, deviceRegistryCountAfter),
            "device_registry_count_changed_or_unavailable");
        }

        const reply = turn.messages.at(-1);
        privateTrace.replies.push({ name, text: reply?.text || "" });
        privateTrace.call_service_events.push({ name, events });
        privateTrace.service_observer_events = nativeObserver.events;
        privateTrace.turn_probes = privateTrace.turn_probes || [];
        privateTrace.turn_probes.push({ name, case_id: currentCaseId, callback_events: callbackEvents,
          current_ui: { busy: turn.busy, status_error: turn.status_error },
          same_document: sameDocument, document_end: endDocument,
          document_events: documentEventsForTurn,
          backend: diagnostics, observer_start: observerStart,
          observer_end: observerAfterReadback, service_events: events });
        persistPrivate();

        chatTurnMayBeActive = true;
        assert.equal(diagnostics.active, false, `${name}_backend_not_idle`);
        chatTurnMayBeActive = false;
        if (expectedService) {
          assert.equal(events.length, 1, `${name}_expected_one_service_call`);
          assert.equal(events[0].event_type, "call_service", `${name}_unexpected_event_type`);
          assert.equal(events[0].domain, "input_boolean", `${name}_wrong_service_domain`);
          assert.equal(events[0].service, expectedService, `${name}_wrong_service`);
          assert.ok(serviceEventTargetsOnlyEntity(events[0], entityId), `${name}_wrong_service_target`);
          assert.equal(events[0].context_user_id, currentOwnerUserId, `${name}_context_mismatch`);
        } else {
          assert.equal(events.length, 0, `${name}_must_not_emit_service_call`);
        }
        report.cases[name] = {
          assistant_replied: true, service_call_count: events.length,
          submission_accepted: true, turn_completed: true, turn_elapsed_ms: callback.elapsed_ms,
          same_document_and_chat: true, backend_idle_confirmed: true,
          connection_identity: observerStart.connection_identity, uninterrupted_observation: true,
          target_matched: Boolean(expectedService && serviceEventTargetsOnlyEntity(events[0], entityId)),
          context_matched: Boolean(expectedService && events[0]?.context_user_id === currentOwnerUserId),
          readback: state.state,
          ...(options.verifyDeviceInventory ? {
            device_registry_count_before: deviceRegistryCountBefore,
            device_registry_count_after: deviceRegistryCountAfter,
            device_registry_count_unchanged: true,
            answer_semantic_review: "MANUAL_REVIEW_REQUIRED",
          } : {}),
        };
        savePublic();
      }

      await sendTurn("turn_on", `Включи только тестовый переключатель ${entityId}. Не меняй другие сущности и после вызова проверь его состояние.`, "on", "turn_on");
      await sendTurn("turn_off", `Выключи только тестовый переключатель ${entityId}. Не меняй другие сущности и после вызова проверь его состояние.`, "off", "turn_off");
      await sendTurn("unknown_target", "Включи устройство с названием «Несуществующая QA цель issue-monitor». Сначала проверь, есть ли оно в Home Assistant. Не выбирай похожее устройство и не выполняй действие, если точной цели нет.", "off", null);
      await sendTurn("device_inventory", "Проверь в Home Assistant, какие устройства сейчас подключены и сколько их. Коротко перечисли их типы. Ничего не подключай, не меняй и не запускай.", "off", null,
        { verifyDeviceInventory: true });
      report.verdict = "ACTIONS_PASS_CLEANUP_PENDING_MANUAL_REVIEW";
        } catch (error) {
        report.verdict = "FAIL";
        report.failure_code = error instanceof Error && /^[a-z0-9_]+$/.test(error.message)
          ? error.message : "e2e_assertion_failed";
        const errorClass = safeErrorClass(error);
        report.failure_stage = currentStage;
        report.failure_error_class = errorClass;
        try {
          await captureFailureSnapshot(page, currentCaseName || "setup", currentPrompt, currentEventOffset,
            currentStage, errorClass);
        } catch {
          report.failure_snapshot = { case_name: currentCaseName || "setup", ui_snapshot_captured: false };
        }
        throw error;
      } finally {
        // withOwnedChromium closes the page after this callback resolves.
        await cleanupWhileBrowserOpen(page);
      }
    });
  } catch (error) {
    setupError = error instanceof Error ? error.message : "e2e_failed";
    if (report.verdict !== "FAIL_CLEANUP") report.verdict = "FAIL";
    report.failure_code = /^[a-z0-9_]+$/.test(setupError) ? setupError : "e2e_assertion_failed";
    report.failure_stage ||= currentStage;
    report.failure_error_class ||= safeErrorClass(error);
  } finally {
    if (nativeObserver) {
      privateTrace.service_observer_events = nativeObserver.events;
      privateTrace.service_observer_before_close = nativeObserver.snapshot();
      try { persistPrivate(); } catch {}
      try { await nativeObserver.close(); } catch {}
    }
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

module.exports = { hasAcceptedTurn, hasContinuousObserver, isReadyToSubmit,
  sameDeviceInventoryCount, shouldDeferFixtureCleanup, hasSameDocument,
  hasNoDocumentNavigation, hasCompletedSendCallbacks, observedEventsIfContinuous,
  serviceEventProjection, serviceEventTargetsOnlyEntity, writePublicReport };

if (require.main === module) {
  main().catch((error) => {
    const code = error instanceof Error && /^[a-z0-9_]+$/.test(error.message)
      ? error.message : "preflight_failed";
    process.stderr.write(`${code}\n`);
    process.exitCode = 1;
  });
}
