'use strict';

// One bounded, chat-only vacuum cycle for an explicitly authorized owner-test window.
// Never use against a personal Home Assistant; never add, remove, or reconfigure a device.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const puppeteer = require('../../node_modules/puppeteer-core');
const WebSocket = require('../../node_modules/ws');
const {withOwnedChromium} = require('/root/tools/chrome-daemon/owned-browser.cjs');
const {NativeHaServiceObserver, hasCompletedSendCallbacks, serviceEventTargetsOnlyEntity} =
  require('./service-call-e2e.cjs');

const ORIGIN = 'https://habab-demo.wpmix.net';
const OWNER_ROUTE = `${ORIGIN}/owner-test`;
const HOME_ROUTE = `${ORIGIN}/home/overview`;
const FEATURE = Object.freeze({STOP: 8, RETURN_HOME: 16, START: 8192});
const LIMIT = Object.freeze({
  ownerConnection: 60000,
  chatIdle: 90000,
  chatAcceptance: 10000,
  chatReply: 150000,
  serviceEvent: 12000,
  startState: 90000,
  stopState: 60000,
  dockState: 240000,
  emergencyState: 120000,
  cleanupTurn: 20000,
});
const PRIVATE_ROOT = '/root/.local/share/habab-owner-test/';
const PUBLIC_ROOT = path.resolve(__dirname) + path.sep;
const SAFE_CODE = /^[a-z0-9_]{1,100}$/;
const LOCAL_WS_HOSTS = new Set(['127.0.0.1', 'localhost', '::1']);

function fail(code, error) {
  const reason = SAFE_CODE.test(code) ? code : 'qa_failed';
  const failure = new Error(reason);
  failure.code = reason;
  if (error instanceof Error) failure.name = /^[A-Za-z0-9_]{1,40}$/.test(error.name) ? error.name : 'Error';
  return failure;
}

function safeErrorName(error) {
  return error instanceof Error && /^[A-Za-z0-9_]{1,40}$/.test(error.name) ? error.name : 'Error';
}

function parseArgs(argv) {
  const values = {};
  for (let index = 2; index < argv.length; index += 1) {
    const key = argv[index];
    if (key === '--run-live') {
      values.runLive = true;
      continue;
    }
    if (!key.startsWith('--') || index + 1 >= argv.length || argv[index + 1].startsWith('--')) {
      throw fail('invalid_arguments');
    }
    values[key.slice(2)] = argv[++index];
  }
  for (const name of ['auth-file', 'runtime-identity', 'expected-image', 'expected-source',
    'expected-runtime', 'expected-product-version', 'expected-core-version', 'native-ws-url',
    'execution-window-id', 'report', 'private-trace']) {
    if (typeof values[name] !== 'string' || !values[name]) throw fail(`missing_${name.replaceAll('-', '_')}`);
  }
  if (!values.runLive) throw fail('requires_explicit_run_live');
  if (!/^[A-Za-z0-9_.:-]{1,80}$/.test(values['execution-window-id'])) throw fail('invalid_execution_window_id');
  if (!/^sha256:[a-f0-9]{64}$/.test(values['expected-image'])) throw fail('invalid_expected_image');
  if (!/^[a-f0-9]{40}$/.test(values['expected-source'])) throw fail('invalid_expected_source');
  const ws = new URL(values['native-ws-url']);
  if (ws.protocol !== 'ws:' || !LOCAL_WS_HOSTS.has(ws.hostname) || ws.pathname !== '/api/websocket' ||
      ws.search || ws.hash || ws.username || ws.password) throw fail('native_ws_must_be_loopback_core');
  values.nativeWsUrl = ws.href;
  values.authPath = path.resolve(values['auth-file']);
  values.identityPath = path.resolve(values['runtime-identity']);
  values.reportPath = path.resolve(values.report);
  values.privateTracePath = path.resolve(values['private-trace']);
  if (!values.reportPath.startsWith(PUBLIC_ROOT) || !values.privateTracePath.startsWith(PRIVATE_ROOT) ||
      values.reportPath === values.privateTracePath) throw fail('output_path_outside_allowed_root');
  if (fs.existsSync(values.reportPath) || fs.existsSync(values.privateTracePath)) throw fail('qa_output_path_preexists');
  return values;
}

function readJson(file, code) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch (error) { throw fail(code, error); }
}

function loadRuntime(options) {
  const authStat = fs.statSync(options.authPath);
  if (!authStat.isFile() || (authStat.mode & 0o077) !== 0) throw fail('auth_file_permissions_invalid');
  const auth = readJson(options.authPath, 'auth_file_unreadable');
  if (typeof auth.access_token !== 'string' || !auth.access_token ||
      typeof auth.client_id !== 'string' || new URL(auth.client_id).origin !== ORIGIN) throw fail('auth_target_mismatch');
  if (auth.base_url && new URL(auth.base_url).origin !== ORIGIN) throw fail('auth_base_url_mismatch');
  const identity = readJson(options.identityPath, 'runtime_identity_unreadable');
  if ((identity.image || identity.image_sha256) !== options['expected-image'] ||
      identity.source !== options['expected-source'] || identity.runtime !== options['expected-runtime']) {
    throw fail('runtime_identity_mismatch');
  }
  return {auth: {access_token: auth.access_token, client_id: auth.client_id,
    expires_in: Number.isFinite(auth.expires_in) ? auth.expires_in : 3600},
  identity: {image: options['expected-image'], source: identity.source, runtime: identity.runtime},
  origin: ORIGIN, base: OWNER_ROUTE, cookieHeader: ''};
}

async function bootstrapOwnerSession(runtime) {
  const cookies = new Map();
  let url = runtime.base;
  for (let hop = 0; hop < 4; hop += 1) {
    const headers = {Authorization: `Bearer ${runtime.auth.access_token}`};
    if (cookies.size) headers.Cookie = [...cookies].map(([key, value]) => `${key}=${value}`).join('; ');
    const response = await fetch(url, {headers, redirect: 'manual'}).catch(() => { throw fail('owner_session_bootstrap_failed'); });
    const responseUrl = new URL(response.url);
    if (responseUrl.origin !== runtime.origin || responseUrl.protocol !== 'https:') throw fail('owner_session_target_rejected');
    for (const header of response.headers.getSetCookie()) {
      const pair = header.split(';', 1)[0];
      const separator = pair.indexOf('=');
      if (separator > 0) cookies.set(pair.slice(0, separator), pair.slice(separator + 1));
    }
    if (response.status >= 300 && response.status < 400) {
      const location = response.headers.get('location');
      if (!location || hop === 3) throw fail('owner_session_redirect_rejected');
      const next = new URL(location, url);
      if (next.origin !== runtime.origin || next.protocol !== 'https:' || next.username || next.password) {
        throw fail('owner_session_redirect_rejected');
      }
      url = next.href;
      continue;
    }
    if (!response.ok) throw fail(`owner_session_bootstrap_failed_${response.status}`);
    runtime.cookieHeader = [...cookies].map(([key, value]) => `${key}=${value}`).join('; ');
    return;
  }
  throw fail('owner_session_redirect_rejected');
}

async function getJson(runtime, url, code) {
  const response = await fetch(url, {headers: {
    Authorization: `Bearer ${runtime.auth.access_token}`, Cookie: runtime.cookieHeader,
  }, redirect: 'manual'}).catch(() => { throw fail(code); });
  if (!response.ok) throw fail(`${code}_${response.status}`);
  return response.json().catch(() => { throw fail(code); });
}

async function readRestState(runtime, entityId) {
  const url = `${runtime.origin}/api/states/${encodeURIComponent(entityId)}`;
  const response = await fetch(url, {headers: {
    Authorization: `Bearer ${runtime.auth.access_token}`, Cookie: runtime.cookieHeader,
  }, redirect: 'manual'}).catch(() => { throw fail('vacuum_state_read_failed'); });
  if (response.status === 404) return null;
  if (!response.ok) throw fail(`vacuum_state_read_failed_${response.status}`);
  const value = await response.json().catch(() => { throw fail('vacuum_state_read_failed'); });
  return {state: value.state, last_changed: value.last_changed || null,
    supported_features: value.attributes?.supported_features};
}

async function preflight(runtime, options) {
  await bootstrapOwnerSession(runtime);
  const status = await getJson(runtime, `${runtime.origin}/api/habab_home/status`, 'runtime_status_read_failed');
  if (status.product !== 'Habab Home' || status.version !== options['expected-product-version'] ||
      status.home_assistant_version !== options['expected-core-version'] || status.status !== 'running') {
    throw fail('runtime_status_mismatch');
  }
  return {product_version: status.version, core_version: status.home_assistant_version,
    status: status.status, image: runtime.identity.image, source: runtime.identity.source,
    runtime: runtime.identity.runtime};
}

function eventMatches(event, entityId, service, ownerId) {
  return event?.event_type === 'call_service' && event.domain === 'vacuum' && event.service === service &&
    serviceEventTargetsOnlyEntity(event, entityId) && event.target_fields?.length === 1 &&
    event.target_fields[0] === 'entity_id' && event.context_user_id === ownerId;
}

const DEVICE_BINDING_FIELDS = Object.freeze(['entity_id', 'entity_registry_id', 'device_id',
  'config_entry_id', 'platform', 'supported_features']);

function sameDeviceBinding(expected, current) {
  return Boolean(expected && current) && DEVICE_BINDING_FIELDS.every((field) =>
    Object.hasOwn(expected, field) && Object.hasOwn(current, field) && expected[field] === current[field]);
}

function hasOwnedStart(events, entityId, ownerId, turn, callbackEvents = []) {
  if (!turn || typeof turn.case_id !== 'string' || !Number.isInteger(turn.event_offset)) return false;
  const started = callbackEvents.filter((event) => event.case_id === turn.case_id && event.kind === 'send_started');
  const accepted = callbackEvents.filter((event) => event.case_id === turn.case_id && event.kind === 'send_accepted');
  if (started.length !== 1 || accepted.length !== 1 || accepted[0].accepted !== true ||
      started[0].session_id !== turn.session_id || started[0].owner_id !== ownerId ||
      started[0].document_marker !== turn.document_marker || started[0].chat_instance_id !== turn.chat_instance_id ||
      accepted[0].session_id !== turn.session_id ||
      accepted[0].owner_id !== ownerId || accepted[0].document_marker !== turn.document_marker ||
      accepted[0].chat_instance_id !== turn.chat_instance_id) return false;
  return events.slice(turn.event_offset).filter((event) => eventMatches(event, entityId, 'start', ownerId)).length === 1;
}

function latestTurnMessageEvidence(messages, expectedText) {
  if (!Array.isArray(messages)) return {latest_user_match: false, assistant_present: false, assistant_reply: ''};
  const latestUserIndex = messages.findLastIndex((message) => message?.role === 'user');
  const latestUser = latestUserIndex >= 0 ? messages[latestUserIndex] : null;
  const assistants = latestUserIndex >= 0 ? messages.slice(latestUserIndex + 1).filter((message) =>
    message?.role === 'assistant' && typeof message.text === 'string' && message.text.trim()) : [];
  return {latest_user_match: latestUser?.text === expectedText,
    assistant_present: assistants.length > 0, assistant_reply: assistants.at(-1)?.text || ''};
}

function hasVerifiedSendCompletion(events, caseId, baseline, messages, expectedText) {
  const completed = events.filter((event) => event.case_id === caseId && event.kind === 'send_completed');
  const currentTurn = latestTurnMessageEvidence(messages, expectedText);
  return hasCompletedSendCallbacks(events, caseId, {document_marker: baseline?.documentMarker,
    chat_instance_id: baseline?.chatInstanceId}) && completed.length === 1 &&
    completed[0].accepted === true && completed[0].latest_user_match === true &&
    completed[0].assistant_present === true && currentTurn.latest_user_match && currentTurn.assistant_present;
}

function canAttemptEmergencyStop({ownedStart, bindingMatches, mappedSession, ownerAdmin, state}) {
  return ownedStart === true && bindingMatches === true && mappedSession === true &&
    ownerAdmin === true && ['cleaning', 'returning', 'paused'].includes(state);
}

function canAttemptEmergencyDock({stopEventVerified, bindingMatches, mappedSession, ownerAdmin, state}) {
  return stopEventVerified === true && bindingMatches === true && mappedSession === true &&
    ownerAdmin === true && ['idle', 'paused'].includes(state);
}

function emergencyRecoveryComplete({stopCall, stoppedState, dockCall, dockedState}) {
  return stopCall?.ws_result === true && stopCall?.event_verified === true &&
    ['idle', 'paused'].includes(stoppedState?.state) && dockCall?.ws_result === true &&
    dockCall?.event_verified === true && dockedState?.state === 'docked';
}

async function observeOneService(observer, offset, entityId, service, ownerId, timeoutMs = LIMIT.serviceEvent) {
  await observer.waitForEventAfter(offset, timeoutMs);
  await waitForQuietEvents(observer);
  const events = observer.events.slice(offset);
  if (events.length !== 1 || !eventMatches(events[0], entityId, service, ownerId)) {
    throw fail(`vacuum_${service}_service_event_mismatch`);
  }
  return events[0];
}

async function waitForQuietEvents(observer, ms = 500) {
  const before = observer.events.length;
  await new Promise((resolve) => setTimeout(resolve, ms));
  if (!observer.snapshot().connected || observer.events.length !== before) {
    throw fail('native_ws_observer_not_quiet');
  }
}

function updateJson(file, value) {
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`, {flag: 'w', mode: 0o600});
  fs.chmodSync(file, 0o600);
}

function deriveDeviceIdentity(states, entities, devices) {
  const vacuums = (Array.isArray(states) ? states : []).filter((state) => state.entity_id?.startsWith('vacuum.'));
  if (vacuums.length !== 1) throw fail('vacuum_entity_count_not_one');
  const state = vacuums[0];
  const entityRows = (Array.isArray(entities) ? entities : []).filter((item) => item.entity_id === state.entity_id);
  if (entityRows.length !== 1) throw fail('vacuum_registry_entity_not_unique');
  const entity = entityRows[0];
  if (!entity.device_id || entity.disabled_by) throw fail('vacuum_registry_device_unavailable');
  const deviceRows = (Array.isArray(devices) ? devices : []).filter((item) => item.id === entity.device_id);
  if (deviceRows.length !== 1) throw fail('vacuum_registry_device_not_unique');
  const device = deviceRows[0];
  const supported = Number(state.attributes?.supported_features);
  const required = FEATURE.STOP | FEATURE.RETURN_HOME | FEATURE.START;
  if (!Number.isInteger(supported) || (supported & required) !== required) {
    throw fail('vacuum_required_commands_not_supported');
  }
  const friendlyName = [device.name_by_user, device.name, entity.name, entity.original_name,
    state.attributes?.friendly_name].find((value) => typeof value === 'string' && value.trim());
  return {entity_id: state.entity_id, state: state.state, supported_features: supported,
    friendly_name: friendlyName?.trim() || null,
    friendly_name_source: friendlyName === device.name_by_user ? 'device.name_by_user' :
      friendlyName === device.name ? 'device.name' : friendlyName === entity.name ? 'entity.name' :
        friendlyName === entity.original_name ? 'entity.original_name' :
          friendlyName ? 'state.friendly_name' : null,
    entity_registry_id: entity.id, device_id: device.id, config_entry_id: entity.config_entry_id || null,
    platform: entity.platform || null, manufacturer: device.manufacturer || null,
    model: device.model || null, device_name: device.name || null,
    device_name_by_user: device.name_by_user || null};
}

async function readDeviceRows(page) {
  return page.evaluate(async () => {
    const chat = window.__hababQaFind('habab-setup-chat');
    if (!chat?.hass?.callWS) throw new Error('ha_connection_missing');
    const [states, entities, devices] = await Promise.all([
      chat.hass.callWS({type: 'get_states'}),
      chat.hass.callWS({type: 'config/entity_registry/list'}),
      chat.hass.callWS({type: 'config/device_registry/list'}),
    ]);
    return {states, entities, devices};
  });
}

async function readDeviceIdentity(page) {
  const rows = await readDeviceRows(page);
  return deriveDeviceIdentity(rows.states, rows.entities, rows.devices);
}

async function readNativeDeviceIdentity(observer) {
  const [states, entities, devices] = await Promise.all([
    observer.command({type: 'get_states'}),
    observer.command({type: 'config/entity_registry/list'}),
    observer.command({type: 'config/device_registry/list'}),
  ]);
  return deriveDeviceIdentity(states, entities, devices);
}

async function readChatState(page) {
  return page.evaluate(() => {
    const chat = window.__hababQaFind('habab-setup-chat');
    const messages = Array.isArray(chat?.messages) ? chat.messages : [];
    const latestUser = [...messages].reverse().find((item) => item.role === 'user');
    return {chat_present: Boolean(chat), chat_connected: Boolean(chat?.isConnected),
      session_id: chat?.sessionId || null, owner_id: chat?.hass?.user?.id || null,
      owner_admin: chat?.hass?.user?.is_admin === true, current_owner: chat?.isCurrentOwnerContext?.() === true,
      busy: Boolean(chat?.busy), running: Boolean(chat?.running), dialog_open: Boolean(chat?.shadowRoot?.querySelector('dialog')?.open),
      draft: chat?.shadowRoot?.querySelector('textarea')?.value || '',
      status_error: Boolean(chat?.shadowRoot?.querySelector('#status')?.classList.contains('error')),
      latest_user_text: latestUser?.text || null, history_length: messages.length,
      path: location.pathname, document_marker: window.__hababVacuumDocumentMarker || null,
      chat_instance: chat ? (chat.__hababVacuumQaId ||= crypto.randomUUID()) : null};
  });
}

async function waitForSettledServiceWorker(page) {
  return waitUi(page, async () => {
    const serviceWorker = navigator.serviceWorker;
    const watch = window.__hababQaServiceWorkerWatch;
    if (!serviceWorker || !watch) return false;
    let registration;
    try { registration = await serviceWorker.getRegistration(); } catch { return false; }
    const controller = serviceWorker.controller;
    if (!registration?.active || registration.active.state !== 'activated' || !controller ||
        controller.state !== 'activated') {
      watch.controller = null;
      watch.stableSince = null;
      return false;
    }
    if (watch.controller !== controller) {
      watch.controller = controller;
      watch.stableSince = performance.now();
    }
    if (performance.now() - watch.lastChange < 8000 ||
        performance.now() - (watch.stableSince || 0) < 8000) return false;
    const baseline = {controller, changeCount: watch.changeCount, scriptUrl: controller.scriptURL,
      registrationScope: registration.scope, stableMs: Math.floor(performance.now() - watch.stableSince)};
    window.__hababQaServiceWorkerBaseline = baseline;
    window.__hababQaServiceWorkerStable = () => {
      const currentWatch = window.__hababQaServiceWorkerWatch;
      return Boolean(currentWatch && navigator.serviceWorker?.controller === baseline.controller &&
        currentWatch.changeCount === baseline.changeCount && baseline.controller.state === 'activated');
    };
    return {controller_ready: true, controller_change_count: watch.changeCount,
      controller_script_path: new URL(controller.scriptURL).pathname, stable_ms: baseline.stableMs};
  }, 120000, undefined, 'service_worker_controller_not_settled', 250);
}

async function waitUi(page, predicate, timeout, args, failureCode, polling = 250) {
  let result;
  try {
    result = await page.waitForFunction(predicate, {timeout, polling}, args);
    return await result.jsonValue();
  } catch {
    throw fail(failureCode);
  } finally {
    await result?.dispose();
  }
}

async function openChatIfClosed(page) {
  await page.evaluate(() => {
    const chat = window.__hababQaFind('habab-setup-chat');
    if (!chat?.shadowRoot?.querySelector('dialog[open]')) chat?.shadowRoot?.querySelector('.launcher')?.click();
  });
  await waitUi(page, () => {
    const chat = window.__hababQaFind('habab-setup-chat');
    return Boolean(chat?.shadowRoot?.querySelector('dialog[open] textarea') && chat.shadowRoot.querySelector('textarea')?.disabled === false);
  }, LIMIT.chatIdle, undefined, 'chat_open_timeout');
}

async function sendVisiblePrompt(page, prompt, expectedSession, expectedOwner, trace, callbackEvents, caseId) {
  const baseline = await page.evaluate(({sessionId, ownerId}) => {
    const chat = window.__hababQaFind('habab-setup-chat');
    const input = chat?.shadowRoot?.querySelector('textarea');
    const send = chat?.shadowRoot?.querySelector('.send');
    const dialog = chat?.shadowRoot?.querySelector('dialog');
    if (!chat?.isConnected || chat !== window.__hababVacuumBaseline?.chat ||
        window.__hababVacuumDocumentMarker !== window.__hababVacuumBaseline?.documentMarker ||
        window.__hababQaServiceWorkerStable?.() !== true || chat.sessionId !== sessionId ||
        chat.hass?.user?.id !== ownerId || chat.hass?.user?.is_admin !== true ||
        chat.isCurrentOwnerContext?.() !== true || chat.busy || !dialog?.open || !input || input.disabled ||
        input.value.trim() || !send || send.disabled || location.pathname !== '/home/overview') {
      throw new Error('chat_submit_precondition_failed');
    }
    return {sessionId: chat.sessionId, ownerId: chat.hass.user.id,
      documentMarker: window.__hababVacuumDocumentMarker,
      chatInstanceId: chat.__hababVacuumQaId};
  }, {sessionId: expectedSession, ownerId: expectedOwner});
  const probe = {case_id: caseId, document_marker: baseline.documentMarker,
    chat_instance_id: baseline.chatInstanceId, session_id: baseline.sessionId, owner_id: baseline.ownerId,
    expected_text: prompt, started: false, accepted: false, completed: false};
  trace.case_id = caseId;
  persistTrace(trace);
  await page.evaluate(({expected}) => {
    const chat = window.__hababQaFind('habab-setup-chat');
    if (!chat || chat !== window.__hababVacuumBaseline?.chat ||
        chat.sessionId !== expected.session_id || chat.hass?.user?.id !== expected.owner_id) {
      throw new Error('chat_probe_identity_mismatch');
    }
    if (!chat.__hababVacuumOriginalSend) {
      chat.__hababVacuumOriginalSend = chat.send;
      chat.send = async function (...args) {
        const active = this.__hababVacuumProbe;
        const submitted = typeof args[0] === 'string' ? args[0].trim() :
          this.shadowRoot?.querySelector('textarea')?.value?.trim();
        if (!active || active.started || submitted !== active.expected_text) {
          return this.__hababVacuumOriginalSend.apply(this, args);
        }
        const emit = (signal) => {
          const receiver = window.__hababVacuumQaSendSignal;
          if (typeof receiver !== 'function') return;
          try { Promise.resolve(receiver(signal)).catch(() => {}); } catch {}
        };
        const startedAt = Date.now();
        const beforeBusy = Boolean(this.busy);
        const beforeCount = this.messages?.length || 0;
        active.started = true;
        emit({kind: 'send_started', case_id: active.case_id,
          document_marker: window.__hababVacuumDocumentMarker,
          chat_instance_id: this.__hababVacuumQaId, session_id: this.sessionId,
          owner_id: this.hass?.user?.id, submitted_length: submitted.length});
        let operation;
        try {
          operation = this.__hababVacuumOriginalSend.apply(this, args);
        } catch (error) {
          emit({kind: 'send_failed', case_id: active.case_id, error_class: error instanceof Error ? error.name : 'UnknownError'});
          throw error;
        }
        const optimistic = this.messages?.at(-1);
        active.accepted = !beforeBusy && this.busy === true && this.messages?.length === beforeCount + 1 &&
          optimistic?.role === 'user' && optimistic.text === submitted;
        emit({kind: 'send_accepted', case_id: active.case_id, document_marker: window.__hababVacuumDocumentMarker,
          chat_instance_id: this.__hababVacuumQaId, session_id: this.sessionId,
          owner_id: this.hass?.user?.id, accepted: active.accepted});
        let errorClass = null;
        try {
          return await operation;
        } catch (error) {
          errorClass = error instanceof Error ? error.name : 'UnknownError';
          throw error;
        } finally {
          const messages = Array.isArray(this.messages) ? this.messages : [];
          const latestUserIndex = messages.findLastIndex((message) => message?.role === 'user');
          const latestUser = latestUserIndex >= 0 ? messages[latestUserIndex] : null;
          const assistantsAfterLatestUser = latestUserIndex >= 0 ? messages.slice(latestUserIndex + 1).filter((message) =>
            message?.role === 'assistant' && typeof message.text === 'string' && message.text.trim()) : [];
          const latestAssistant = assistantsAfterLatestUser.at(-1);
          const statusError = Boolean(this.shadowRoot?.querySelector('#status')?.classList.contains('error'));
          const completed = {kind: 'send_completed', case_id: active.case_id,
            document_marker: window.__hababVacuumDocumentMarker,
            chat_instance_id: this.__hababVacuumQaId, session_id: this.sessionId,
            owner_id: this.hass?.user?.id, accepted: active.accepted, completed: true,
            final_busy: Boolean(this.busy), status_error: statusError,
            latest_user_match: latestUser?.text === active.expected_text,
            assistant_present: assistantsAfterLatestUser.length > 0,
            assistant_reply: String(latestAssistant?.text || ''),
            assistant_length: String(latestAssistant?.text || '').length,
            elapsed_ms: Date.now() - startedAt, error_class: errorClass};
          active.completed = true;
          emit(completed);
        }
      };
    }
    chat.__hababVacuumProbe = expected;
  }, {expected: probe});

  const input = await page.evaluateHandle(() => window.__hababQaFind('habab-setup-chat')?.shadowRoot?.querySelector('textarea'));
  try {
    const element = input.asElement();
    if (!element) throw fail('chat_textarea_missing');
    await element.click();
    await element.type(prompt, {delay: 2});
    await element.press('Enter');
  } finally {
    await input.dispose();
  }
  const waitForSignal = async (kind, timeoutMs, code) => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (callbackEvents.some((event) => event.case_id === caseId && event.kind === 'send_failed')) throw fail('chat_send_failed');
      const event = callbackEvents.find((item) => item.case_id === caseId && item.kind === kind);
      if (event) return event;
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    throw fail(code);
  };
  const accepted = await waitForSignal('send_accepted', 5000, 'chat_send_acceptance_timeout');
  if (accepted.accepted !== true || accepted.document_marker !== baseline.documentMarker ||
      accepted.chat_instance_id !== baseline.chatInstanceId || accepted.session_id !== baseline.sessionId ||
      accepted.owner_id !== baseline.ownerId) throw fail('chat_send_acceptance_mismatch');
  trace.accepted = true;
  const completed = await waitForSignal('send_completed', LIMIT.chatReply, 'chat_send_completion_timeout');
  const callbackSnapshot = callbackEvents.filter((event) => event.case_id === caseId);
  const turn = await page.evaluate(({sessionId, ownerId, chatInstanceId, documentMarker}) => {
    const chat = window.__hababQaFind('habab-setup-chat');
    const messages = Array.isArray(chat?.messages) ? chat.messages.map((message) => ({
      role: message?.role, text: typeof message?.text === 'string' ? message.text : '',
    })) : [];
    return {messages,
      same_chat: chat === window.__hababVacuumBaseline?.chat && chat?.__hababVacuumQaId === chatInstanceId,
      same_document: window.__hababVacuumDocumentMarker === documentMarker,
      same_service_worker: window.__hababQaServiceWorkerStable?.() === true,
      same_session: chat?.sessionId === sessionId,
      same_owner: chat?.hass?.user?.id === ownerId && chat?.hass?.user?.is_admin === true &&
        chat?.isCurrentOwnerContext?.() === true,
      idle: chat?.busy === false && chat?.running === false,
      no_ui_error: !chat?.shadowRoot?.querySelector('#status')?.classList.contains('error'),
      path: location.pathname};
  }, {sessionId: expectedSession, ownerId: expectedOwner,
    chatInstanceId: baseline.chatInstanceId, documentMarker: baseline.documentMarker});
  const messageEvidence = latestTurnMessageEvidence(turn.messages, prompt);
  if (!hasVerifiedSendCompletion(callbackSnapshot, caseId, baseline, turn.messages, prompt) ||
      completed.document_marker !== baseline.documentMarker || completed.chat_instance_id !== baseline.chatInstanceId ||
      completed.session_id !== baseline.sessionId || completed.owner_id !== baseline.ownerId ||
      completed.status_error || completed.final_busy || !messageEvidence.latest_user_match ||
      !messageEvidence.assistant_present) {
    throw fail('chat_send_completion_mismatch');
  }
  turn.latest_user_match = messageEvidence.latest_user_match;
  turn.assistant_present = messageEvidence.assistant_present;
  turn.assistant_reply = messageEvidence.assistant_reply;
  delete turn.messages;
  if (!turn.same_chat || !turn.same_document || !turn.same_service_worker || !turn.same_session ||
      !turn.same_owner || !turn.idle || !turn.no_ui_error || turn.path !== '/home/overview') {
    throw fail('chat_turn_identity_or_completion_mismatch');
  }
  turn.assistant_reply = messageEvidence.assistant_reply;
  turn.assistant_reply_length = completed.assistant_length;
  trace.turn = turn;
  trace.completed = true;
  return turn;
}

async function readVacuumIdentity(page) {
  const identity = await readDeviceIdentity(page);
  if (!/^vacuum\.[a-z0-9_]+$/.test(identity.entity_id) || identity.state !== 'idle') throw fail('vacuum_not_unique_idle_target');
  return identity;
}

async function waitForState(runtime, entityId, allowedStates, timeoutMs, trace, stage) {
  const deadline = Date.now() + timeoutMs;
  let lastState = null;
  while (Date.now() < deadline) {
    const read = await readRestState(runtime, entityId);
    if (!read) throw fail('vacuum_entity_disappeared');
    if (read.state !== lastState) {
      trace.state_reads.push({stage, at: new Date().toISOString(), state: read.state,
        last_changed: read.last_changed});
      lastState = read.state;
      persistTrace(trace);
    }
    if (allowedStates.includes(read.state)) return read;
    if (read.state === 'unavailable' || read.state === 'unknown' || read.state === 'error') {
      throw fail(`vacuum_${stage}_terminal_state_${read.state}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw fail(`vacuum_${stage}_state_timeout`);
}

let activePrivateTracePath;
function persistTrace(trace) {
  if (!activePrivateTracePath) return;
  updateJson(activePrivateTracePath, trace);
}

async function runChatAction({page, runtime, observer, identity, ownerId, sessionId, trace, report, callbackEvents}, spec) {
  const start = {observer: observer.snapshot(), event_offset: observer.events.length,
    state: await readRestState(runtime, identity.entity_id)};
  if (!start.observer.connected || !start.observer.owner_matches || start.observer.disconnected_count !== 0 ||
      !start.state || !spec.fromStates.includes(start.state.state)) throw fail(`vacuum_${spec.name}_precondition_mismatch`);
  const turnTrace = {name: spec.name, expected_service: spec.service, accepted: false, completed: false,
    prompt: spec.prompt, service_event: null, state_before: start.state.state, state_after: null};
  turnTrace.case_id = crypto.randomUUID();
  turnTrace.event_offset = start.event_offset;
  turnTrace.session_id = sessionId;
  turnTrace.owner_id = ownerId;
  turnTrace.document_marker = trace.document_marker;
  turnTrace.chat_instance_id = trace.chat_instance;
  trace.turns.push(turnTrace);
  trace.current_turn = turnTrace;
  trace.last_prompt = spec.prompt;
  persistTrace(trace);
  const turn = await sendVisiblePrompt(page, spec.prompt, sessionId, ownerId, turnTrace, callbackEvents, turnTrace.case_id);
  await observeOneService(observer, start.event_offset, identity.entity_id, spec.service, ownerId);
  turnTrace.assistant_reply_length = turn.assistant_reply.length;
  persistTrace(trace);
  const state = await waitForState(runtime, identity.entity_id, spec.states, spec.stateTimeout, trace, spec.name);
  await waitForQuietEvents(observer);
  const serviceEvents = observer.events.slice(start.event_offset);
  if (serviceEvents.length !== 1 || !eventMatches(serviceEvents[0], identity.entity_id, spec.service, ownerId)) {
    throw fail(`vacuum_${spec.service}_service_event_mismatch`);
  }
  turnTrace.service_event = serviceEvents[0];
  turnTrace.state_after = state.state;
  report.actions[spec.name] = {chat_prompt_accepted: true, chat_reply_present: true,
    exact_target_service_event: true, authenticated_user_context: true, expected_state_reached: true,
    state: state.state};
  persistTrace(trace);
  return {turn, state};
}

function makePrompt(action, entityId) {
  if (action === 'start') return `Запусти уборку этим единственным роботом-пылесосом через Home Assistant: ${entityId}. Только этот пылесос; не меняй настройки и не запускай другие устройства.`;
  if (action === 'stop') return `Останови уборку только у этого робота-пылесоса через Home Assistant: ${entityId}. Не управляй другими устройствами.`;
  return `Отправь этот робот-пылесос на зарядную базу через Home Assistant: ${entityId}. Не управляй другими устройствами.`;
}

async function stopOwnedPendingTurn(page, ownerId, sessionId, turn, callbackEvents) {
  const accepted = turn && callbackEvents.filter((event) => event.case_id === turn.case_id &&
    event.kind === 'send_accepted' && event.accepted === true && event.session_id === sessionId &&
    event.owner_id === ownerId && event.document_marker === turn.document_marker &&
    event.chat_instance_id === turn.chat_instance_id);
  if (!accepted || accepted.length !== 1 || turn.owner_id !== ownerId || turn.session_id !== sessionId) {
    return {ownership_confirmed: false, idle: false, stop_clicked: false};
  }
  const stop = await page.evaluateHandle(({expectedOwner, expectedSession, expectedChatInstance,
    expectedDocument, prompt}) => {
    const chat = window.__hababQaFind('habab-setup-chat');
    const messages = Array.isArray(chat?.messages) ? chat.messages : [];
    const latestUser = [...messages].reverse().find((item) => item.role === 'user');
    if (!chat || chat !== window.__hababVacuumBaseline?.chat ||
        chat.__hababVacuumQaId !== expectedChatInstance || window.__hababVacuumDocumentMarker !== expectedDocument ||
        window.__hababVacuumDocumentMarker !== window.__hababVacuumBaseline?.documentMarker ||
        window.__hababQaServiceWorkerStable?.() !== true ||
        chat.sessionId !== expectedSession || chat.hass?.user?.id !== expectedOwner ||
        chat.hass?.user?.is_admin !== true || chat.isCurrentOwnerContext?.() !== true ||
        latestUser?.text !== prompt) return null;
    if (!chat.busy && !chat.running) return {idle: true};
    const button = chat.shadowRoot?.querySelector('.stop');
    return button && !button.disabled && !button.hidden ? button : null;
  }, {expectedOwner: ownerId, expectedSession: sessionId, expectedChatInstance: turn.chat_instance_id,
    expectedDocument: turn.document_marker, prompt: turn.prompt});
  try {
    const value = await stop.jsonValue().catch(() => null);
    if (value?.idle) return {ownership_confirmed: true, idle: true, stop_clicked: false};
    const element = stop.asElement();
    if (!element) return {ownership_confirmed: false, idle: false, stop_clicked: false};
    await element.click();
    await waitUi(page, ({expectedOwner, expectedSession, expectedChatInstance, expectedDocument, prompt}) => {
      const chat = window.__hababQaFind('habab-setup-chat');
      const latestUser = [...(chat?.messages || [])].reverse().find((item) => item.role === 'user');
      return chat === window.__hababVacuumBaseline?.chat &&
        chat?.__hababVacuumQaId === expectedChatInstance && window.__hababVacuumDocumentMarker === expectedDocument &&
        window.__hababVacuumDocumentMarker === window.__hababVacuumBaseline?.documentMarker &&
        window.__hababQaServiceWorkerStable?.() === true &&
        chat?.sessionId === expectedSession && chat?.hass?.user?.id === expectedOwner &&
        chat?.hass?.user?.is_admin === true && chat?.isCurrentOwnerContext?.() === true &&
        latestUser?.text === prompt && !chat.busy && !chat.running;
    }, LIMIT.cleanupTurn, {expectedOwner: ownerId, expectedSession: sessionId,
      expectedChatInstance: turn.chat_instance_id, expectedDocument: turn.document_marker, prompt: turn.prompt},
    'owned_chat_stop_timeout', 250);
    return {ownership_confirmed: true, idle: true, stop_clicked: true};
  } catch {
    return {ownership_confirmed: false, idle: false, stop_clicked: true};
  } finally {
    await stop.dispose();
  }
}

async function emergencyService(observer, trace, identity, ownerId, service) {
  const offset = observer.events.length;
  const result = {service, attempted: true, ws_result: false, event_verified: false};
  try {
    await observer.command({type: 'call_service', domain: 'vacuum', service,
      target: {entity_id: identity.entity_id}}, {timeoutMs: 15000});
    result.ws_result = true;
  } catch (error) {
    result.error_code = SAFE_CODE.test(error?.code || '') ? error.code : 'operator_service_failed';
  }
  try {
    await observer.waitForEventAfter(offset, 5000);
    await waitForQuietEvents(observer);
    const events = observer.events.slice(offset);
    result.event_verified = events.length === 1 && eventMatches(events[0], identity.entity_id, service, ownerId);
    result.events = events;
  } catch {
    result.event_verified = false;
  }
  trace.emergency_services.push(result);
  persistTrace(trace);
  return result;
}

async function readMappedSession(observer, ownerId, sessionId) {
  const snapshot = observer.snapshot();
  if (!snapshot.connected || !snapshot.owner_matches || observer.userId !== ownerId) return null;
  let diagnostic;
  try {
    diagnostic = await observer.command({type: 'habab_home/agent/diagnostics', session_id: sessionId},
      {timeoutMs: 5000});
  } catch {
    return null;
  }
  if (diagnostic?.session_id !== sessionId || diagnostic.runtime_status !== 'running' ||
      typeof diagnostic.active !== 'boolean') return null;
  return {owner_admin: true, mapped_session: true, active: diagnostic.active};
}

async function ensureMappedSessionIdle(observer, ownerId, sessionId) {
  let mapping = await readMappedSession(observer, ownerId, sessionId);
  if (!mapping) return false;
  if (mapping.active) {
    try {
      await observer.command({type: 'habab_home/agent/abort', session_id: sessionId}, {timeoutMs: 10000});
    } catch {
      return false;
    }
    const deadline = Date.now() + LIMIT.cleanupTurn;
    while (Date.now() < deadline) {
      mapping = await readMappedSession(observer, ownerId, sessionId);
      if (!mapping) return false;
      if (!mapping.active) return true;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    return false;
  }
  return true;
}

async function readEmergencyBinding(observer, runtime, expected, ownerId, sessionId, allowedStates,
  requireInactive = true) {
  const mapping = await readMappedSession(observer, ownerId, sessionId);
  if (!mapping || (requireInactive && mapping.active)) return null;
  const current = await readNativeDeviceIdentity(observer).catch(() => null);
  if (!current || !sameDeviceBinding(expected, current) || !Number.isInteger(current.supported_features) ||
      (current.supported_features & (FEATURE.STOP | FEATURE.RETURN_HOME | FEATURE.START)) !==
        (FEATURE.STOP | FEATURE.RETURN_HOME | FEATURE.START)) return null;
  const state = await readRestState(runtime, expected.entity_id).catch(() => null);
  if (!state || state.state !== current.state || state.supported_features !== current.supported_features ||
      !allowedStates.includes(state.state)) return null;
  const snapshot = observer.snapshot();
  if (!snapshot.connected || !snapshot.owner_matches || observer.userId !== ownerId) return null;
  return {identity: current, state: state.state, owner_admin: mapping.owner_admin,
    mapped_session: mapping.mapped_session, session_active: mapping.active};
}

async function emergencyRecovery({page, runtime, observer, identity, ownerId, sessionId, trace, report}) {
  const current = await readRestState(runtime, identity.entity_id).catch(() => null);
  if (!current) {
    report.emergency_recovery = {attempted: false, complete: false, manual_recovery_required: true};
    return;
  }
  if (current.state === 'docked') {
    report.emergency_recovery = {attempted: false, already_docked: true, complete: true};
    return;
  }
  const recoveryPreflight = await readEmergencyBinding(observer, runtime, identity, ownerId, sessionId,
    ['cleaning', 'returning', 'idle', 'paused'], false);
  if (!recoveryPreflight) {
    report.emergency_recovery = {attempted: false, complete: false, target_binding_revalidated: false,
      manual_recovery_required: true};
    return;
  }
  const lastTurn = trace.current_turn;
  const stopped = await stopOwnedPendingTurn(page, ownerId, sessionId, lastTurn, trace.send_callbacks || [])
    .catch(() => ({ownership_confirmed: false, idle: false, stop_clicked: false}));
  const sessionIdle = await ensureMappedSessionIdle(observer, ownerId, sessionId);
  trace.chat_stop_before_emergency = {...stopped, mapped_session_idle: sessionIdle};
  persistTrace(trace);
  if (!sessionIdle) {
    report.emergency_recovery = {attempted: false, complete: false, manual_recovery_required: true};
    return;
  }
  const stopBefore = await readEmergencyBinding(observer, runtime, identity, ownerId, sessionId,
    ['cleaning', 'returning', 'idle', 'paused']);
  if (!stopBefore || !canAttemptEmergencyStop({ownedStart: true, bindingMatches: true,
    mappedSession: stopBefore.mapped_session, ownerAdmin: stopBefore.owner_admin, state: stopBefore.state})) {
    report.emergency_recovery = {attempted: false, complete: false, manual_recovery_required: true,
      target_binding_revalidated: false};
    return;
  }
  const stopCall = await emergencyService(observer, trace, stopBefore.identity, ownerId, 'stop');
  if (!stopCall.ws_result || !stopCall.event_verified) {
    report.emergency_recovery = {attempted: true, complete: false, exact_target_stop_sent: stopCall.ws_result,
      stop_event_verified: stopCall.event_verified, dock_skipped_after_stop_failure: true,
      manual_recovery_required: true};
    return;
  }
  let stoppedState = null;
  try { stoppedState = await waitForState(runtime, identity.entity_id, ['idle', 'paused'], LIMIT.emergencyState, trace, 'emergency_stop'); } catch {}
  if (!stoppedState) {
    report.emergency_recovery = {attempted: true, complete: false, exact_target_stop_sent: true,
      stop_event_verified: true, stop_state_reached: false, dock_skipped_after_stop_failure: true,
      manual_recovery_required: true};
    return;
  }
  const dockBefore = await readEmergencyBinding(observer, runtime, identity, ownerId, sessionId, ['idle', 'paused']);
  if (!dockBefore || !canAttemptEmergencyDock({stopEventVerified: true,
    bindingMatches: sameDeviceBinding(identity, dockBefore.identity), mappedSession: dockBefore.mapped_session,
    ownerAdmin: dockBefore.owner_admin, state: dockBefore.state})) {
    report.emergency_recovery = {attempted: true, complete: false, exact_target_stop_sent: true,
      stop_event_verified: true, stop_state_reached: true, dock_skipped_after_stop_failure: true,
      manual_recovery_required: true};
    return;
  }
  const dockCall = await emergencyService(observer, trace, dockBefore.identity, ownerId, 'return_to_base');
  let dockedState = null;
  try { dockedState = await waitForState(runtime, identity.entity_id, ['docked'], LIMIT.dockState, trace, 'emergency_return_home'); } catch {}
  const complete = emergencyRecoveryComplete({stopCall, stoppedState, dockCall, dockedState});
  report.emergency_recovery = {attempted: true, chat_turn_stopped_first: true,
    exact_target_stop_sent: stopCall.ws_result, stop_event_verified: stopCall.event_verified,
    stop_state_reached: Boolean(stoppedState), exact_target_return_sent: dockCall.ws_result,
    return_event_verified: dockCall.event_verified, docked_state_reached: Boolean(dockedState),
    complete, manual_recovery_required: !complete};
  persistTrace(trace);
}

async function main() {
  let options;
  let runtime;
  let reportPath;
  let report;
  let trace;
  let privateReserved = false;
  let publicReserved = false;
  let stage = 'preflight';
  let observer;
  let lastPage;
  let ownerId;
  let sessionId;
  let target;

  try {
    options = parseArgs(process.argv);
    activePrivateTracePath = options.privateTracePath;
    runtime = loadRuntime(options);
    const identity = await preflight(runtime, options);
    reportPath = options.reportPath;
    report = {checked_at: new Date().toISOString(), verdict: 'IN_PROGRESS',
      target_alias: 'accepted-owner-test', execution_window_id: options['execution-window-id'],
      release: {image: identity.image, source: identity.source, runtime: identity.runtime,
        product_version: identity.product_version, core_version: identity.core_version},
      route: '/home/overview', physical_motion_observed: false,
      device_preflight: {one_available_vacuum: false, starts_idle: false, supported_commands: false},
      actions: {}, emergency_recovery: {attempted: false, complete: null},
      cleanup: {owned_browser_closed: false}, failure: null};
    trace = {started_at: report.checked_at, stage, execution_window_id: options['execution-window-id'],
      target_origin: runtime.origin, turns: [], service_events: [], state_reads: [],
      emergency_services: [], page_error_count: 0, failed_request_count: 0, http_error_count: 0};
    fs.mkdirSync(path.dirname(reportPath), {recursive: true});
    fs.writeFileSync(reportPath, '', {flag: 'wx', mode: 0o600});
    publicReserved = true;
    fs.writeFileSync(options.privateTracePath, '', {flag: 'wx', mode: 0o600});
    fs.chmodSync(options.privateTracePath, 0o600);
    privateReserved = true;
    updateJson(reportPath, report);
    persistTrace(trace);

    stage = 'browser_startup';
    trace.stage = stage;
    persistTrace(trace);
    await withOwnedChromium(puppeteer, {taskTimeoutMs: 900000,
      args: ['--disable-dev-shm-usage'], defaultViewport: {width: 1280, height: 800}}, async ({page}) => {
      lastPage = page;
      const callbackEvents = trace.send_callbacks = [];
      await page.exposeFunction('__hababVacuumQaSendSignal', (signal) => {
        const permitted = ['kind', 'case_id', 'document_marker', 'chat_instance_id', 'session_id', 'owner_id',
          'accepted', 'completed', 'final_busy', 'status_error', 'latest_user_match', 'assistant_present',
          'assistant_reply', 'assistant_length', 'elapsed_ms', 'error_class', 'submitted_length'];
        if (!signal || !['send_started', 'send_accepted', 'send_completed', 'send_failed'].includes(signal.kind)) return;
        const event = {at: new Date().toISOString()};
        for (const key of permitted) {
          const value = signal[key];
          if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' || value === null) {
            event[key] = value;
          }
        }
        callbackEvents.push(event);
        persistTrace(trace);
      });
      try {
        page.on('pageerror', () => { trace.page_error_count += 1; });
        page.on('requestfailed', () => { trace.failed_request_count += 1; });
        page.on('response', (response) => { if (response.status() >= 400) trace.http_error_count += 1; });
        await page.evaluateOnNewDocument((tokens) => {
          window.__hababQaFind = (selector, root = document) => {
            const direct = root.querySelector(selector);
            if (direct) return direct;
            for (const element of root.querySelectorAll('*')) {
              if (!element.shadowRoot) continue;
              const nested = window.__hababQaFind(selector, element.shadowRoot);
              if (nested) return nested;
            }
            return null;
          };
          window.__hababVacuumDocumentMarker = crypto.randomUUID();
          if (navigator.serviceWorker) {
            const watch = {changeCount: 0, lastChange: performance.now(), controller: null, stableSince: null};
            navigator.serviceWorker.addEventListener('controllerchange', () => {
              watch.changeCount += 1;
              watch.lastChange = performance.now();
              watch.controller = null;
              watch.stableSince = null;
            });
            window.__hababQaServiceWorkerWatch = watch;
          }
          localStorage.setItem('hassTokens', JSON.stringify({...tokens, hassUrl: location.origin,
            clientId: tokens.client_id, expires: Date.now() + tokens.expires_in * 1000}));
          localStorage.setItem('selectedLanguage', JSON.stringify('ru'));
        }, runtime.auth);
        stage = 'owner_route';
        trace.stage = stage;
        await page.goto(OWNER_ROUTE, {waitUntil: 'networkidle2', timeout: 60000});
        await page.goto(HOME_ROUTE, {waitUntil: 'networkidle2', timeout: 60000});
        report.route_pathname = await page.evaluate(() => location.pathname);
        trace.route_pathname = report.route_pathname;
        if (report.route_pathname !== '/home/overview') throw fail('home_route_mismatch');
        stage = 'service_worker_settle';
        trace.stage = stage;
        const serviceWorker = await waitForSettledServiceWorker(page);
        report.service_worker = {controller_ready_before_commands: true,
          settled_without_controllerchange_for_ms: serviceWorker.stable_ms,
          controller_change_count_before_commands: serviceWorker.controller_change_count,
          this_wait_is_not_reload_fix_proof: true};
        trace.service_worker = serviceWorker;
        persistTrace(trace);
        stage = 'chat_ready';
        trace.stage = stage;
        await waitUi(page, () => {
          const chat = window.__hababQaFind('habab-setup-chat');
          return chat?.hass?.user?.is_admin === true && chat.hass.callWS && chat.hass.config?.version;
        }, LIMIT.ownerConnection, undefined, 'owner_chat_connection_timeout');
        await openChatIfClosed(page);
        await waitUi(page, () => {
          const chat = window.__hababQaFind('habab-setup-chat');
          return Boolean(chat?.sessionId && !chat.busy && !chat.running &&
            chat.shadowRoot?.querySelector('dialog[open] textarea')?.disabled === false);
        }, LIMIT.chatIdle, undefined, 'chat_initial_idle_timeout');
        const chat = await readChatState(page);
        if (!chat.chat_present || !chat.chat_connected || !chat.owner_admin || !chat.current_owner ||
            typeof chat.owner_id !== 'string' || !chat.owner_id ||
            chat.path !== '/home/overview' || chat.busy || chat.running || !chat.dialog_open || chat.draft.trim() ||
            typeof chat.session_id !== 'string' || !chat.session_id) throw fail('chat_preflight_mismatch');
        ownerId = chat.owner_id;
        sessionId = chat.session_id;
        report.owner_admin = true;
        report.chat = {same_session_across_cycle: true, initial_idle: true, initial_draft_empty: true,
          initial_history_length: chat.history_length};
        trace.owner_id = ownerId;
        trace.session_id = sessionId;
        trace.chat_instance = chat.chat_instance;
        trace.document_marker = chat.document_marker;
        trace.initial_history_length = chat.history_length;
        await page.evaluate(({expectedSession, expectedOwner, expectedChatInstance}) => {
          const element = window.__hababQaFind('habab-setup-chat');
          if (!element || element.sessionId !== expectedSession || element.hass?.user?.id !== expectedOwner ||
              element.__hababVacuumQaId !== expectedChatInstance) throw new Error('chat_baseline_changed');
          window.__hababVacuumBaseline = {chat: element, sessionId: expectedSession, ownerId: expectedOwner,
            chatInstance: expectedChatInstance, documentMarker: window.__hababVacuumDocumentMarker};
        }, {expectedSession: sessionId, expectedOwner: ownerId, expectedChatInstance: chat.chat_instance});

        stage = 'vacuum_registry_preflight';
        trace.stage = stage;
        target = await readVacuumIdentity(page).catch((error) => {
          throw fail(SAFE_CODE.test(error?.message || '') ? error.message : 'vacuum_registry_read_failed');
        });
        const restBefore = await readRestState(runtime, target.entity_id);
        if (!restBefore || restBefore.state !== 'idle' || restBefore.supported_features !== target.supported_features) {
          throw fail('vacuum_native_readback_precondition_mismatch');
        }
        report.device_preflight = {one_available_vacuum: true, starts_idle: true,
          supported_commands: true, friendly_name_available: Boolean(target.friendly_name),
          supported_features: target.supported_features};
        trace.device = target;
        trace.initial_state = restBefore;
        trace.expected_states = {start: 'cleaning', stop: ['idle', 'paused'], dock: 'docked'};
        persistTrace(trace);

        stage = 'native_observer';
        trace.stage = stage;
        observer = new NativeHaServiceObserver(runtime.auth.access_token,
          () => new WebSocket(options.nativeWsUrl));
        await observer.ready;
        await observer.authenticateOwner(ownerId);
        await observer.subscribeCallService();
        let observerEventBoundary = observer.events.length;
        report.native_observer = {authenticated_same_admin: true, continuous: true};
        trace.observer_start = observer.snapshot();
        persistTrace(trace);

        const actions = [
          {name: 'start', service: 'start', fromStates: ['idle'], states: ['cleaning'], stateTimeout: LIMIT.startState},
          {name: 'stop', service: 'stop', fromStates: ['cleaning'], states: ['idle', 'paused'], stateTimeout: LIMIT.stopState},
          {name: 'return_home', service: 'return_to_base', fromStates: ['idle', 'paused'], states: ['docked'], stateTimeout: LIMIT.dockState},
        ];
        for (const spec of actions) {
          if (observer.events.length !== observerEventBoundary) throw fail('unexpected_service_event_between_chat_turns');
          stage = `chat_${spec.name}`;
          trace.stage = stage;
          spec.prompt = makePrompt(spec.name, target.entity_id);
          await runChatAction({page, runtime, observer, identity: target, ownerId, sessionId, trace, report,
            callbackEvents}, spec);
          trace.service_events = observer.events;
          trace.observer_last = observer.snapshot();
          observerEventBoundary = observer.events.length;
          persistTrace(trace);
        }
        const finalState = await readRestState(runtime, target.entity_id);
        if (!finalState || finalState.state !== 'docked') throw fail('vacuum_final_state_not_docked');
        await waitForQuietEvents(observer, 1000);
        if (observer.events.length !== observerEventBoundary) throw fail('unexpected_service_event_after_cycle');
        if (!observer.snapshot().same_connection || !observer.snapshot().connected ||
            !observer.snapshot().owner_matches || observer.snapshot().disconnected_count !== 0) {
          throw fail('native_observer_not_continuous');
        }
        report.final_state = {docked: true};
        report.verdict = 'PASS_PENDING_MANUAL_REVIEW';
        report.manual_review = {assistant_reply_semantics: 'pending_private_trace_review',
          physical_motion_observed: false};
        trace.final_state = finalState;
        trace.service_events = observer.events;
        trace.observer_end = observer.snapshot();
        trace.stage = 'complete';
        persistTrace(trace);
      } catch (error) {
        const code = SAFE_CODE.test(error?.code || '') ? error.code :
          SAFE_CODE.test(error?.message || '') ? error.message : 'qa_failed';
        report.failure = {stage, code, source_error_name: safeErrorName(error)};
        trace.failure = report.failure;
        trace.stage = stage;
        if (observer) trace.service_events = observer.events;
        persistTrace(trace);
        const startTurn = trace.turns?.find((turn) => turn.name === 'start');
        const startEventObserved = observer && target && ownerId
          ? hasOwnedStart(observer.events, target.entity_id, ownerId, startTurn, trace.send_callbacks || [])
          : false;
        const stateSuggestsMovement = target && runtime
          ? await readRestState(runtime, target.entity_id).then((state) => ['cleaning', 'returning'].includes(state?.state)).catch(() => false)
          : false;
        if (startEventObserved && page && target && ownerId && sessionId && observer) {
          try {
            await emergencyRecovery({page, runtime, observer, identity: target, ownerId, sessionId, trace, report});
          } catch {
            report.emergency_recovery = {attempted: true, complete: false, manual_recovery_required: true};
            persistTrace(trace);
          }
        } else if (stateSuggestsMovement) {
          report.emergency_recovery = {attempted: false, complete: false,
            manual_recovery_required: true, reason: 'movement_without_owned_start_event'};
        }
        throw error;
      } finally {
        if (observer) {
          trace.observer_end = observer.snapshot();
          trace.service_events = observer.events;
          await observer.close();
          report.cleanup.native_observer_closed = true;
        }
      }
    });
    report.cleanup.owned_browser_closed = true;
  } catch (error) {
    if (report) {
      if (lastPage) report.cleanup.owned_browser_closed = true;
      if (!report.failure) report.failure = {stage, code: SAFE_CODE.test(error?.code || '') ? error.code : 'qa_failed',
        source_error_name: safeErrorName(error)};
      if (report.verdict !== 'PASS_PENDING_MANUAL_REVIEW') report.verdict = 'FAIL';
      trace && (trace.failure = report.failure);
    } else {
      process.stderr.write(`${JSON.stringify({verdict: 'BLOCKED', code: SAFE_CODE.test(error?.code || '') ? error.code : 'preflight_failed',
        source_error_name: safeErrorName(error)})}\n`);
      process.exitCode = 1;
    }
  } finally {
    if (trace && privateReserved) {
      trace.finished_at = new Date().toISOString();
      trace.stage = stage;
      try { persistTrace(trace); } catch {}
    }
    if (report && publicReserved) {
      report.finished_at = new Date().toISOString();
      updateJson(reportPath, report);
      process.stdout.write(`${JSON.stringify({verdict: report.verdict, stage,
        actions: report.actions, final_state: report.final_state || null,
        emergency_recovery: report.emergency_recovery, report: reportPath})}\n`);
      if (report.verdict === 'FAIL' || report.verdict === 'BLOCKED') process.exitCode = 1;
    }
  }
}

module.exports = {sameDeviceBinding, hasOwnedStart, latestTurnMessageEvidence, hasVerifiedSendCompletion,
  canAttemptEmergencyStop, canAttemptEmergencyDock, emergencyRecoveryComplete, deriveDeviceIdentity};

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`${JSON.stringify({verdict: 'BLOCKED', code: SAFE_CODE.test(error?.code || '') ? error.code : 'runner_failed',
      source_error_name: safeErrorName(error)})}\n`);
    process.exitCode = 1;
  });
}
