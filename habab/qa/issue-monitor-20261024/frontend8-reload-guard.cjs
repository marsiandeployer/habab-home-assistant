'use strict';

// Live QA runner for frontend.8's dirty-provider service-worker reload guard.
// Live execution requires a unique --attempt label and explicit --execute after root GO.
// This runner never calls chat.send(), changes config, invokes HA services, or
// dispatches synthetic service-worker events.

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const WS = require('/root/homeassistent/habab/node_modules/ws');
const puppeteer = require('/root/homeassistent/habab/node_modules/puppeteer-core');
const {withOwnedChromium} = require('/root/tools/chrome-daemon/owned-browser.cjs');
const {clearFocusedTextareaWithKeyboard} = require('./frontend8-reload-guard-keyboard.cjs');

const ROOT = '/root/.local/share/habab-owner-test';
const AUTH_PATH = path.join(ROOT, 'qa-current-auth.json');
const CANDIDATE_PATH = '/root/homeassistent/habab/qa/issue-monitor-20261024/frontend8-candidate-image-independent.json';
const ORIGIN = 'https://habab-demo.wpmix.net';
const OWNER_ROUTE = `${ORIGIN}/owner-test`;
const HOME_ROUTE = `${ORIGIN}/home/overview`;
const EXPECTED_IMAGE = 'sha256:fc6062a20172affd5a7218d0586e29580ccbb0e876596ae10ba13360059bc928';
const EXPECTED_SOURCE = 'f803277c68511622e3ad780d10a8a046cb9ef478';
const EXPECTED_INIT_HOTFIX_SHA256 = 'b594844a055cab6b677788e69720f66450227474743b3db0b786ab7e97d980d1';
const EXPECTED_SETUP_CHAT_SHA256 = '2870095b9086708aca680e98062b3bb4ab64332b4494db33973fea88bb19559c';
const EXPECTED_STRINGS_SHA256 = '3a0f512c8999adc855888387efe3434a5428df1c9c8f8f93a1e4315b155681ca';
const EXPECTED_VOICE_SHA256 = 'c0e4a5ca4c4871b5fec56dbd8d371cd76727430210b61c0a2319de5af4d226fe';
const EXPECTED_ASSISTANT_URL = `/habab-home/${EXPECTED_SETUP_CHAT_SHA256.slice(0, 16)}.js`;
const DRAFT = 'QA draft: preserve across the first real service-worker activation';
const execute = process.argv.includes('--execute');
const attemptIndex = process.argv.indexOf('--attempt');
const attemptLabel = attemptIndex >= 0 ? process.argv[attemptIndex + 1] : undefined;
const attemptLabelValid = typeof attemptLabel === 'string' && /^[a-z0-9][a-z0-9-]{0,31}$/.test(attemptLabel);

if (process.argv.includes('--self-test')) {
  verifyDraftBaselineShape();
  process.stdout.write('BASELINE_SHAPE_TEST_PASS\n');
  process.exit(0);
}

if (!execute || !attemptLabelValid) {
  process.stderr.write('Usage after root GO: node frontend8-reload-guard.cjs --attempt <unique-label> --execute\n');
  process.exit(2);
}

const PRIVATE_DIR = path.join(ROOT, `frontend8-reload-guard-${attemptLabel}`);
const PUBLIC_PATH = path.join(__dirname, `frontend8-reload-guard-${attemptLabel}.json`);
const PRIVATE_PATH = path.join(PRIVATE_DIR, `private-${attemptLabel}.json`);

if (fs.existsSync(PRIVATE_DIR) || fs.existsSync(PUBLIC_PATH)) {
  process.stderr.write('Existing attempt receipt found; refusing to overwrite.\n');
  process.exit(2);
}
fs.mkdirSync(PRIVATE_DIR, {recursive: true, mode: 0o700});
fs.chmodSync(PRIVATE_DIR, 0o700);

const report = {
  attempt: attemptLabel, started_at: new Date().toISOString(), status: 'IN_PROGRESS',
  stage: 'runtime_preflight', route: '/home/overview',
  target: `${ORIGIN}/owner-test`,
  events: [], page_errors: [], failed_responses: [],
  module_responses: [],
  main_frame_documents: [], in_document_navigations: [],
  websocket_command_types: [], controller_changes: [], dirty_state_changes: [],
};
const waiters = new Set();
let activeObserver = null;
let finalized = false;
const now = () => new Date().toISOString();
const persist = () => fs.writeFileSync(PRIVATE_PATH, JSON.stringify(report, null, 2) + '\n', {mode: 0o600});
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function safePath(url) {
  try { return new URL(url).pathname; } catch (_) { return null; }
}

function event(type, data = {}) {
  const item = {at: now(), type, ...data};
  report.events.push(item);
  if (type === 'controllerchange') report.controller_changes.push(item);
  if (type === 'dirty_state_changed') report.dirty_state_changes.push(item);
  for (const waiter of [...waiters]) {
    if (waiter.predicate(item)) {
      waiters.delete(waiter);
      clearTimeout(waiter.timer);
      waiter.resolve(item);
    }
  }
  persist();
}

function waitEvent(predicate, timeoutMs, code) {
  const promise = new Promise((resolve, reject) => {
    const existing = report.events.find(predicate);
    if (existing) { resolve(existing); return; }
    const waiter = {predicate, resolve, reject, timer: null};
    waiter.timer = setTimeout(() => {
      waiters.delete(waiter);
      reject(new Error(code));
    }, timeoutMs);
    waiters.add(waiter);
  });
  promise.catch(() => {});
  return promise;
}

function draftBaselineFrom(preflight, swState) {
  if (!preflight || typeof preflight.owner_id !== 'string' || !preflight.owner_id ||
      typeof preflight.session_id !== 'string' || !preflight.session_id || preflight.owner_admin !== true ||
      typeof preflight.marker !== 'string' || !preflight.marker || !swState ||
      swState.ready !== true || typeof swState.marker !== 'string' || !swState.marker ||
      swState.marker !== preflight.marker || swState.installing_state !== 'installing' ||
      swState.active_state !== null || swState.controller_change_count !== 0 || swState.too_late === true) {
    throw new Error('pre_draft_baseline_invalid');
  }
  return {marker: swState.marker, owner_id: preflight.owner_id, session_id: preflight.session_id};
}

function verifyDraftBaselineShape() {
  const preflight = {marker: 'same-document', owner_id: 'owner-baseline', owner_admin: true, session_id: 'session-baseline'};
  const swState = {ready: true, marker: 'same-document', installing_state: 'installing', active_state: null,
    controller_change_count: 0};
  assert.deepEqual(draftBaselineFrom(preflight, swState), {
    marker: 'same-document', owner_id: 'owner-baseline', session_id: 'session-baseline',
  });
  assert.throws(() => draftBaselineFrom({...preflight, session_id: undefined}, swState), /pre_draft_baseline_invalid/);
}
verifyDraftBaselineShape();

function runtimeIdentity() {
  const candidate = JSON.parse(fs.readFileSync(CANDIDATE_PATH, 'utf8'));
  const inspected = execFileSync('docker', ['inspect', 'habab-owner-test', '--format', '{{.Image}}|{{.State.Running}}'],
    {encoding: 'utf8', timeout: 10000}).trim().split('|');
  const image = inspected[0] || null;
  const running = inspected[1] === 'true';
  const mountedHashes = execFileSync('docker', ['exec', 'habab-owner-test', 'sha256sum',
    '/usr/src/homeassistant/homeassistant/components/habab_home/__init__.py',
    '/usr/src/homeassistant/homeassistant/components/habab_home/frontend/setup-chat.js',
    '/usr/src/homeassistant/homeassistant/components/habab_home/frontend/strings.js',
    '/usr/src/homeassistant/homeassistant/components/habab_home/frontend/voice.js'],
  {encoding: 'utf8', timeout: 10000}).trim().split('\n').map(line => line.trim().split(/\s+/));
  const sourceHashes = Object.fromEntries(mountedHashes.map(([hash, file]) => [path.basename(file), hash]));
  const candidateMatches = candidate.image === EXPECTED_IMAGE && candidate.source === EXPECTED_SOURCE &&
    candidate.branch === 'qa/owner-frontend8-20261025' && candidate.baked_frontend === '20260930.4+habab.8' &&
    candidate.baked_component_files_matched === 44 && candidate.baked_tests_passed === 80 && candidate.test_source_overlay === false;
  const hotfixMatches = sourceHashes['__init__.py'] === EXPECTED_INIT_HOTFIX_SHA256 &&
    sourceHashes['setup-chat.js'] === EXPECTED_SETUP_CHAT_SHA256 && sourceHashes['strings.js'] === EXPECTED_STRINGS_SHA256 &&
    sourceHashes['voice.js'] === EXPECTED_VOICE_SHA256;
  return {candidate_image: candidate.image, candidate_source: candidate.source,
    container_image: image, running, candidate_matches: candidateMatches, hotfix_hashes: sourceHashes,
    hotfix_matches: hotfixMatches, hotfix_init_sha256: sourceHashes['__init__.py'] || null,
    hotfix_annotation: 'forward hotfix to async_register_static_paths; image digest/source remain fc606/f803',
    live_image_matches: image === EXPECTED_IMAGE && running};
}

class ReadOnlyServiceObserver {
  constructor(token) {
    this.ws = new WS('ws://127.0.0.1:8395/api/websocket');
    this.token = token;
    this.nextId = 1;
    this.pending = new Map();
    this.eventId = null;
    this.callServiceCount = 0;
    this.ready = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('observer_auth_timeout')), 15000);
      this.ws.on('error', () => { clearTimeout(timer); reject(new Error('observer_transport_error')); });
      this.ws.on('message', raw => {
        let message;
        try { message = JSON.parse(raw.toString()); } catch (_) { return; }
        if (message.type === 'auth_required') {
          this.ws.send(JSON.stringify({type: 'auth', access_token: this.token}));
        } else if (message.type === 'auth_ok') {
          clearTimeout(timer); resolve();
        } else if (message.type === 'auth_invalid') {
          clearTimeout(timer); reject(new Error('observer_auth_invalid'));
        } else if (message.type === 'event' && message.id === this.eventId) {
          this.callServiceCount += 1;
        } else if (message.type === 'result' && this.pending.has(message.id)) {
          const row = this.pending.get(message.id); this.pending.delete(message.id);
          if (message.success) row.resolve(message.result); else row.reject(new Error('observer_command_failed'));
        }
      });
    });
  }
  async subscribe() {
    await this.ready;
    const id = this.nextId++;
    this.eventId = id;
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('observer_subscribe_timeout')); }, 12000);
      this.pending.set(id, {resolve: value => {clearTimeout(timer); resolve(value);}, reject: error => {clearTimeout(timer); reject(error);}});
      this.ws.send(JSON.stringify({id, type: 'subscribe_events', event_type: 'call_service'}));
    });
    return true;
  }
  async unsubscribe() {
    if (this.eventId === null) return;
    const subscription = this.eventId;
    this.eventId = null;
    const id = this.nextId++;
    await new Promise(resolve => {
      const timer = setTimeout(resolve, 2000);
      this.pending.set(id, {resolve: () => {clearTimeout(timer); resolve();}, reject: () => {clearTimeout(timer); resolve();}});
      this.ws.send(JSON.stringify({id, type: 'unsubscribe_events', subscription}));
    });
  }
  close() { try { this.ws.close(); } catch (_) {} }
}

function publicSummary() {
  const dirtyInput = report.draft?.dirty_after_input === true;
  const retained = report.after_activation?.draft_retained === true &&
    report.after_activation?.same_chat === true && report.after_activation?.same_chat_instance === true &&
    report.after_activation?.same_document === true && report.after_activation?.same_owner === true &&
    report.after_activation?.same_session === true && report.after_activation?.global_dirty === true;
  const clean = report.after_clear?.global_clean === true;
  const reloadedAfterClean = report.after_reload?.same_owner === true && report.after_reload?.same_session === true &&
    report.after_reload?.draft_empty === true && report.after_reload?.new_document === true &&
    report.deferred_reload?.occurred_after_global_clean === true && report.after_reload?.diagnostics_active === false;
  const noEarlyNavigation = (report.dirty_phase_navigation_count ?? 0) === 0;
  const noForbiddenCommands = (report.model_run_frame_count ?? 0) === 0 && (report.forbidden_write_frame_count ?? 0) === 0;
  const trustedControllerChange = report.controller_changes.some(item => item.trusted === true);
  const allChecks = report.runtime_identity_verified === true && report.provider_api_ready === true &&
    report.bootstrap_ready === true && dirtyInput && retained && clean && reloadedAfterClean && trustedControllerChange &&
    noEarlyNavigation && noForbiddenCommands && report.call_service_count === 0;
  const status = report.status === 'PASS' && allChecks ? 'PASS' :
    report.status === 'IN_PROGRESS' ? 'NOT_RUN_OR_INCOMPLETE' : report.status;
  return {
    attempt: report.attempt, checked_at: report.finished_at || now(), target: report.target,
    expected_build: {image_digest: EXPECTED_IMAGE, source_commit: EXPECTED_SOURCE},
    observed_build: report.runtime_identity ? {image_matches: report.runtime_identity.live_image_matches,
      base_candidate_receipt_matches: report.runtime_identity.candidate_matches,
      forward_hotfix_hashes_match: report.runtime_identity.hotfix_matches,
      init_py_sha256: report.runtime_identity.hotfix_init_sha256} : null,
    status, test: 'real service-worker activation while chat draft is dirty, then deferred reload after clean',
    results: {
      chat_bootstrap_ready: report.bootstrap_ready === true,
      draft_baseline_shape_verified: report.draft_baseline_shape_verified === true,
      hashed_chat_entry_200: report.bootstrap?.expected_hashed_entry_200 === true,
      relative_strings_and_voice_modules_200: report.bootstrap?.relative_module_200 === true,
      habab_home_module_404_count: report.bootstrap?.module_404_count ?? null,
      provider_api_ready: report.provider_api_ready === true,
      draft_dirty_through_real_controllerchange: dirtyInput && retained,
      controllerchange_count: report.controller_changes.length,
      controllerchange_trusted_count: report.controller_changes.filter(item => item.trusted === true).length,
      top_document_reload_while_dirty: !noEarlyNavigation,
      draft_retained_after_activation: retained,
      global_dirty_cleared_by_real_input: clean,
      clear_input_diagnostic: report.after_clear_input ? {
        draft_length: report.after_clear_input.draft_length,
        field_focused_before_keys: report.clear_focus?.field_focused === true,
        chat_busy: report.after_clear_input.chat_busy,
        voice_busy: report.after_clear_input.voice_busy,
        active_voice: report.after_clear_input.active_voice,
        pending_voice_text_length: report.after_clear_input.pending_voice_text_length,
        global_dirty_after_input: report.after_clear_input.global_dirty,
        other_dirty_provider_count: report.after_clear_input.other_dirty_provider_count,
        trusted_input_event_count: report.clear_keyboard_events?.filter(item => item.is_trusted).length ?? 0,
      } : null,
      deferred_reload_after_clean: reloadedAfterClean,
      same_owner_session_after_reload: report.after_reload?.same_owner === true && report.after_reload?.same_session === true,
      model_run_frames: report.model_run_frame_count ?? null,
      config_write_frames: report.forbidden_write_frame_count ?? null,
      call_service_events: report.call_service_count ?? null,
      page_errors: report.page_errors?.length ?? null,
      failed_responses: report.failed_responses?.length ?? null,
      native_editor_coexistence_browser_coverage: false,
    },
    limitation: 'This browser run exercises the chat provider only; native editor coexistence is covered by source unit tests, not this browser scenario.',
  };
}

async function main() {
  persist();
  const authStat = fs.statSync(AUTH_PATH);
  if (Date.now() - authStat.mtimeMs > 20 * 60 * 1000) throw new Error('auth_file_too_old');
  const auth = JSON.parse(fs.readFileSync(AUTH_PATH, 'utf8'));
  if (!auth.access_token || !auth.expires_in || auth.expires_in > 3600) throw new Error('auth_shape_invalid');
  report.runtime_identity = runtimeIdentity();
  if (!report.runtime_identity.candidate_matches || !report.runtime_identity.live_image_matches || !report.runtime_identity.hotfix_matches) {
    throw new Error('runtime_identity_mismatch');
  }
  report.runtime_identity_verified = true;

  const observer = new ReadOnlyServiceObserver(auth.access_token);
  activeObserver = observer;
  await observer.subscribe();
  report.observer_ready = true;
  await withOwnedChromium(puppeteer, {
    taskTimeoutMs: 420000,
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
    defaultViewport: {width: 1280, height: 800},
  }, async ({page}) => {
      const cdp = await page.target().createCDPSession();
      await cdp.send('Page.enable');
      await cdp.send('Runtime.enable');
      await cdp.send('Network.enable');
      const frameTree = await cdp.send('Page.getFrameTree');
      const mainFrameId = frameTree.frameTree.frame.id;
      page.on('pageerror', error => report.page_errors.push({at: now(), name: error?.name || 'Error'}));
      page.on('response', response => {
        const pathname = safePath(response.url());
        if (response.status() >= 400) report.failed_responses.push({at: now(), path: pathname, status: response.status()});
        if (pathname && (/^\/habab-home\/[^/]+\.js$/.test(pathname) ||
            /^\/habab-home\/(?:strings|voice)\.js$/.test(pathname))) {
          report.module_responses.push({at: now(), path: pathname, status: response.status()});
        }
      });
      page.on('requestfailed', request => report.failed_responses.push({at: now(), path: safePath(request.url()), failure: 'request_failed'}));
      page.on('framenavigated', frame => {
        if (frame === page.mainFrame()) report.main_frame_documents.push({at: now(), path: safePath(frame.url()), source: 'puppeteer_frame'});
      });
      cdp.on('Page.frameNavigated', payload => {
        if (!payload.frame?.parentId) {
          const navigation = {at: now(), path: safePath(payload.frame.url), loader_id: payload.frame.loaderId || null, source: 'cdp_frame'};
          report.main_frame_documents.push(navigation);
          event('main_document_navigation', {path: navigation.path, loader_id: navigation.loader_id});
        }
      });
      cdp.on('Page.navigatedWithinDocument', payload => report.in_document_navigations.push({at: now(), path: safePath(payload.url)}));
      cdp.on('Page.frameRequestedNavigation', payload => report.events.push({at: now(), type: 'frame_requested_navigation',
        main: payload.frameId === mainFrameId, path: safePath(payload.url), reason: payload.reason || null}));
      cdp.on('Page.frameScheduledNavigation', payload => report.events.push({at: now(), type: 'frame_scheduled_navigation',
        path: safePath(payload.url), reason: payload.reason || null}));
      cdp.on('Runtime.executionContextsCleared', () => report.events.push({at: now(), type: 'js_contexts_cleared'}));
      cdp.on('Network.requestWillBeSent', payload => {
        if (payload.type === 'Document') report.events.push({at: now(), type: 'document_request',
          path: safePath(payload.request.url), method: payload.request.method, initiator: payload.initiator?.type || null});
      });
      cdp.on('Network.webSocketFrameSent', payload => {
        const raw = payload.response?.payloadData;
        if (typeof raw !== 'string') return;
        try {
          const parsed = JSON.parse(raw);
          const type = typeof parsed.type === 'string' ? parsed.type : null;
          if (!type) return;
          report.websocket_command_types.push(type);
          if (type === 'habab_home/agent/run') report.model_run_frame_count = (report.model_run_frame_count || 0) + 1;
          if (type === 'call_service' || /^config_entries\/flow\/(?:init|configure|abort)$/.test(type) ||
              /^config\/.+\/(?:create|update|delete|remove|enable|disable)$/.test(type) ||
              /^homeassistant\/(?:restart|stop)$/.test(type)) {
            report.forbidden_write_frame_count = (report.forbidden_write_frame_count || 0) + 1;
          }
        } catch (_) {}
      });
      await page.exposeFunction('__reloadGuardQaRecord', payload => {
        if (!payload || typeof payload !== 'object') return;
        const permitted = ['kind', 'marker', 'path', 'trusted', 'controller_state', 'active_state',
          'installing_state', 'waiting_state', 'change_count', 'global_dirty', 'draft_length',
          'draft_sha256', 'has_controller', 'registration_present', 'is_trusted', 'selection_start',
          'selection_end', 'field_focused', 'shadow_active_tag', 'document_active_tag', 'chat_busy',
          'voice_busy', 'active_voice', 'pending_voice_text_length'];
        const data = {};
        for (const key of permitted) {
          const value = payload[key];
          if (typeof value === 'string' || typeof value === 'boolean' || typeof value === 'number' || value === null) data[key] = value;
        }
        event(data.kind || 'page_event', data);
      });
      await page.evaluateOnNewDocument(tokens => {
        window.__reloadGuardDocumentMarker = crypto.randomUUID();
        window.__reloadGuardWatch = {controllerChanges: 0, registrations: null, lastControllerPath: null};
        const emit = data => { try { void window.__reloadGuardQaRecord?.({...data, marker: window.__reloadGuardDocumentMarker}); } catch (_) {} };
        const hashText = async value => {
          const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
          return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')).join('');
        };
        const inputState = async field => ({draft_length: field?.value?.length ?? 0,
          draft_sha256: await hashText(field?.value || ''), selection_start: field?.selectionStart ?? null,
          selection_end: field?.selectionEnd ?? null, field_focused: !!field && field.matches(':focus')});
        const chatState = async () => {
          const chat = document.querySelector('habab-setup-chat');
          const field = chat?.shadowRoot?.querySelector('textarea');
          const input = await inputState(field);
          return {...input, global_dirty: window.isDirtyState === true, chat_busy: !!chat?.busy,
            voice_busy: !!chat?.voiceBusy, active_voice: chat?.voice !== undefined,
            pending_voice_text_length: typeof chat?.pendingVoiceText === 'string' ? chat.pendingVoiceText.length : 0,
            shadow_active_tag: chat?.shadowRoot?.activeElement?.localName || null,
            document_active_tag: document.activeElement?.localName || null};
        };
        window.addEventListener('beforeunload', () => emit({kind: 'beforeunload', global_dirty: window.isDirtyState === true,
          path: location.pathname}), {once: true});
        window.addEventListener('dirty-state-changed', async event => emit({kind: 'dirty_state_changed',
          ...(await chatState()), global_dirty: event.detail?.isDirty === true}), false);
        document.addEventListener('input', async event => {
          const field = event.composedPath().find(node => node instanceof HTMLTextAreaElement);
          if (field) emit({kind: 'chat_textarea_input', draft_length: field.value.length,
            selection_start: field.selectionStart, selection_end: field.selectionEnd,
            field_focused: field.matches(':focus'), is_trusted: event.isTrusted === true});
        }, true);
        if ('serviceWorker' in navigator) {
          navigator.serviceWorker.addEventListener('controllerchange', event => {
            const watch = window.__reloadGuardWatch;
            watch.controllerChanges += 1;
            watch.lastControllerPath = (() => { try { return new URL(navigator.serviceWorker.controller?.scriptURL || '').pathname; } catch (_) { return null; } })();
            emit({kind: 'controllerchange', trusted: event.isTrusted === true, change_count: watch.controllerChanges,
              has_controller: !!navigator.serviceWorker.controller, controller_state: navigator.serviceWorker.controller?.state || null,
              path: watch.lastControllerPath});
          });
          let previous = '';
          const sample = async () => {
            try {
              const registration = await navigator.serviceWorker.getRegistration();
              const controller = navigator.serviceWorker.controller;
              const state = {registration_present: !!registration, has_controller: !!controller,
                controller_state: controller?.state || null, active_state: registration?.active?.state || null,
                installing_state: registration?.installing?.state || null, waiting_state: registration?.waiting?.state || null,
                path: (() => { try { return new URL(controller?.scriptURL || registration?.installing?.scriptURL || '').pathname; } catch (_) { return null; } })()};
              const key = JSON.stringify(state);
              if (key !== previous) { previous = key; window.__reloadGuardWatch.registrations = state; emit({kind: 'sw_registration_state', ...state}); }
            } catch (_) { emit({kind: 'sw_registration_read_error'}); }
            setTimeout(sample, 250);
          };
          void sample();
        }
        localStorage.setItem('hassTokens', JSON.stringify({...tokens, hassUrl: location.origin,
          clientId: tokens.client_id, expires: Date.now() + tokens.expires_in * 1000}));
        localStorage.setItem('selectedLanguage', JSON.stringify('ru'));
      }, auth);

      report.stage = 'owner_route'; persist();
      await page.goto(OWNER_ROUTE, {waitUntil: 'networkidle2', timeout: 60000});
      report.owner_route_path = await page.evaluate(() => location.pathname);
      report.stage = 'home_route'; persist();
      await page.goto(HOME_ROUTE, {waitUntil: 'networkidle2', timeout: 60000});
      report.home_route_path = await page.evaluate(() => location.pathname);
      if (report.home_route_path !== '/home/overview') throw new Error('home_route_mismatch');
      report.stage = 'chat_provider_ready'; persist();
      await page.waitForFunction(() => {
        const chat = document.querySelector('habab-setup-chat');
        return chat?.hass?.user?.is_admin === true && chat.hass.callWS && chat.sessionId &&
          typeof window.haRegisterDirtyStateProvider === 'function' &&
          typeof chat.reloadStateProvider?.setDirty === 'function' && window.isDirtyState === false;
      }, {timeout: 60000, polling: 200});
      report.bootstrap = await page.evaluate(() => {
        const chat = document.querySelector('habab-setup-chat');
        return {path: location.pathname, chat_defined: typeof customElements.get('habab-setup-chat') === 'function',
          chat_rendered: !!chat && !!chat.shadowRoot?.querySelector('textarea'),
          provider_api_ready: typeof window.haRegisterDirtyStateProvider === 'function' &&
            typeof chat?.reloadStateProvider?.setDirty === 'function'};
      });
      report.bootstrap.expected_hashed_entry = EXPECTED_ASSISTANT_URL;
      report.bootstrap.expected_hashed_entry_200 = report.module_responses.some(row =>
        row.path === EXPECTED_ASSISTANT_URL && row.status === 200);
      report.bootstrap.relative_module_200 = ['strings.js', 'voice.js'].every(file =>
        report.module_responses.some(row => row.path === `/habab-home/${file}` && row.status === 200));
      report.bootstrap.module_404_count = report.failed_responses.filter(row =>
        row.status === 404 && row.path?.startsWith('/habab-home/')).length;
      report.bootstrap_ready = report.bootstrap.chat_defined && report.bootstrap.chat_rendered &&
        report.bootstrap.provider_api_ready && report.bootstrap.expected_hashed_entry_200 &&
        report.bootstrap.relative_module_200 && report.bootstrap.module_404_count === 0;
      persist();
      process.stdout.write(JSON.stringify({milestone: 'BOOTSTRAP_READY', build_identity_verified: true,
        hotfix_matches: report.runtime_identity.hotfix_matches, chat_rendered: report.bootstrap.chat_rendered,
        hashed_entry_status: report.bootstrap.expected_hashed_entry_200 ? 200 : null,
        strings_status_200: report.module_responses.some(row => row.path === '/habab-home/strings.js' && row.status === 200),
        voice_status_200: report.module_responses.some(row => row.path === '/habab-home/voice.js' && row.status === 200),
        habab_home_404_count: report.bootstrap.module_404_count}) + '\n');
      if (!report.bootstrap_ready) throw new Error('chat_bootstrap_assets_not_ready');
      let state = await page.evaluate(() => {
        const chat = document.querySelector('habab-setup-chat');
        const field = chat.shadowRoot.querySelector('textarea');
        return {path: location.pathname, marker: window.__reloadGuardDocumentMarker,
          owner_id: chat.hass.user?.id || null, owner_admin: chat.hass.user?.is_admin === true,
          session_id: chat.sessionId, chat_connected: chat.isConnected,
          chat_instance_id: (window.__reloadGuardChatInstanceId = crypto.randomUUID()),
          chat_ref_saved: (window.__reloadGuardChatRef = chat) === chat,
          provider_api_ready: typeof window.haRegisterDirtyStateProvider === 'function' &&
            typeof chat.reloadStateProvider?.setDirty === 'function',
          global_dirty: window.isDirtyState === true, draft: field.value,
          dialog_open: !!chat.shadowRoot.querySelector('dialog')?.open,
          history_count: Array.isArray(chat.messages) ? chat.messages.length : null,
          last_message_signature: (() => { const m = chat.messages?.at(-1); return m ? `${m.role}:${String(m.text || '').length}` : null; })(),
          busy: !!chat.busy, running: !!chat.running};
      });
      if (!state.dialog_open) {
        const launcher = await page.evaluate(() => {
          const rect = document.querySelector('habab-setup-chat').shadowRoot.querySelector('.launcher').getBoundingClientRect();
          return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, visible: rect.width > 0 && rect.height > 0};
        });
        if (!launcher.visible) throw new Error('chat_launcher_not_visible');
        await page.mouse.click(launcher.x, launcher.y);
        await page.waitForFunction(() => document.querySelector('habab-setup-chat')?.shadowRoot?.querySelector('dialog')?.open === true,
          {timeout: 5000});
      }
      const diagnostics = await page.evaluate(async () => {
        const chat = document.querySelector('habab-setup-chat');
        const result = await chat.hass.callWS({type: 'habab_home/agent/diagnostics', session_id: chat.sessionId});
        return {active: result.active, runtime_status: result.runtime_status};
      });
      report.preflight = {...state, dialog_open: true, diagnostics};
      report.provider_api_ready = state.provider_api_ready;
      if (!state.owner_admin || !state.session_id || !state.chat_connected || state.global_dirty || state.draft || state.busy || state.running ||
          diagnostics.active !== false) throw new Error('chat_preflight_not_clean');

      report.stage = 'wait_pre_activation_window'; persist();
      const preActivation = await page.waitForFunction(async () => {
        const watch = window.__reloadGuardWatch;
        if (!watch || watch.controllerChanges > 0 || navigator.serviceWorker?.controller) return {too_late: true};
        let registration = null;
        try { registration = await navigator.serviceWorker?.getRegistration(); } catch (_) { return false; }
        if (!registration) return false;
        if (registration.active?.state === 'activated' || registration.waiting?.state === 'activated') return {too_late: true};
        if (registration.installing?.state === 'installing') return {ready: true,
          installing_state: registration.installing.state, active_state: registration.active?.state || null,
          controller_change_count: watch.controllerChanges, marker: window.__reloadGuardDocumentMarker};
        return false;
      }, {timeout: 45000, polling: 200});
      const preActivationValue = await preActivation.jsonValue(); await preActivation.dispose();
      report.sw_before_draft = preActivationValue;
      if (!preActivationValue?.ready || preActivationValue.too_late) throw new Error('first_sw_activation_preceded_draft');
      const expectedDraftBaseline = draftBaselineFrom(report.preflight, preActivationValue);
      report.draft_baseline_shape_verified = true;

      const dirtyStartEvent = waitEvent(item => item.type === 'dirty_state_changed' && item.global_dirty === true, 5000, 'chat_provider_did_not_publish_dirty');
      report.stage = 'set_unsent_draft'; persist();
      const draftResult = await page.evaluate(({draft, expected}) => {
        const chat = document.querySelector('habab-setup-chat');
        const field = chat.shadowRoot.querySelector('textarea');
        if (!field || !chat.reloadStateProvider || typeof chat.reloadStateProvider.setDirty !== 'function' ||
            window.isDirtyState !== false || navigator.serviceWorker.controller ||
            window.__reloadGuardWatch?.controllerChanges !== 0 ||
            window.__reloadGuardDocumentMarker !== expected.marker || chat.sessionId !== expected.session_id ||
            chat.hass.user?.id !== expected.owner_id || chat.hass.user?.is_admin !== true) {
          throw new Error('pre_draft_guard_changed');
        }
        window.__reloadGuardChatRef = chat;
        field.focus();
        return {marker: window.__reloadGuardDocumentMarker, session_id: chat.sessionId,
          owner_id: chat.hass.user.id,
          api_ready: typeof window.haRegisterDirtyStateProvider === 'function' && typeof chat.reloadStateProvider?.setDirty === 'function',
          chat_connected: chat.isConnected, provider_change_count: window.__reloadGuardWatch?.controllerChanges || 0};
      }, {draft: DRAFT, expected: expectedDraftBaseline});
      await page.keyboard.type(DRAFT, {delay: 5});
      const draftObserved = await page.evaluate(draft => {
        const chat = document.querySelector('habab-setup-chat');
        const field = chat.shadowRoot.querySelector('textarea');
        return {marker: window.__reloadGuardDocumentMarker, session_id: chat.sessionId,
          owner_id: chat.hass.user?.id || null,
          draft_length: field.value.length, draft_matches: field.value === draft, global_dirty: window.isDirtyState === true,
          api_ready: typeof window.haRegisterDirtyStateProvider === 'function' && typeof chat.reloadStateProvider?.setDirty === 'function',
          chat_connected: chat.isConnected, has_controller: !!navigator.serviceWorker?.controller,
          provider_change_count: window.__reloadGuardWatch?.controllerChanges || 0};
      }, DRAFT);
      report.draft = {...draftResult, ...draftObserved};
      report.draft.draft_digest = crypto.createHash('sha256').update(DRAFT).digest('hex');
      report.draft.dirty_after_input = draftObserved.global_dirty && draftObserved.api_ready &&
        draftObserved.draft_matches && draftObserved.draft_length === DRAFT.length &&
        draftObserved.marker === expectedDraftBaseline.marker && draftResult.marker === expectedDraftBaseline.marker &&
        draftObserved.session_id === expectedDraftBaseline.session_id && draftResult.session_id === expectedDraftBaseline.session_id &&
        draftObserved.owner_id === expectedDraftBaseline.owner_id && draftResult.owner_id === expectedDraftBaseline.owner_id &&
        !draftObserved.has_controller &&
        draftObserved.provider_change_count === 0;
      report.draft_set_at = now();
      await dirtyStartEvent;
      if (!report.draft.dirty_after_input) throw new Error('chat_provider_did_not_mark_dirty');
      report.stage = 'wait_real_controllerchange_while_dirty'; persist();
      const controllerEvent = await waitEvent(item => item.type === 'controllerchange', 150000, 'real_controllerchange_timeout');
      report.controller_event = {at: controllerEvent.at, trusted: controllerEvent.trusted === true,
        controller_state: controllerEvent.controller_state, change_count: controllerEvent.change_count,
        document_marker: controllerEvent.marker || null};
      if (!controllerEvent.trusted) throw new Error('controllerchange_not_native_event');
      const dirtyNavigationCount = report.main_frame_documents.filter(item => Date.parse(item.at) >= Date.parse(report.draft_set_at)).length;
      report.dirty_phase_navigation_count = dirtyNavigationCount;
      if (dirtyNavigationCount !== 0) throw new Error('document_navigated_while_dirty');
      await delay(1500);
      state = await page.evaluate(draft => {
        const chat = document.querySelector('habab-setup-chat');
        const field = chat?.shadowRoot?.querySelector('textarea');
        return {marker: window.__reloadGuardDocumentMarker, same_chat: !!window.__reloadGuardChatRef && window.__reloadGuardChatRef === chat,
          chat_instance_id: window.__reloadGuardChatInstanceId || null,
          owner_id: chat?.hass?.user?.id || null, session_id: chat?.sessionId || null,
          provider_api_ready: typeof window.haRegisterDirtyStateProvider === 'function' && typeof chat?.reloadStateProvider?.setDirty === 'function',
          global_dirty: window.isDirtyState === true, draft_retained: field?.value === draft,
          draft_length: field?.value?.length ?? null, busy: !!chat?.busy, running: !!chat?.running,
          controller_changes: window.__reloadGuardWatch?.controllerChanges || 0};
      }, DRAFT);
      report.after_activation = {...state, draft_hash: report.draft.draft_digest,
        same_document: state.marker === report.preflight.marker, same_session: state.session_id === report.preflight.session_id,
        same_owner: state.owner_id === report.preflight.owner_id,
        same_chat_instance: state.chat_instance_id === report.preflight.chat_instance_id};
      if (!report.after_activation.same_chat || !report.after_activation.same_document || !report.after_activation.same_session ||
          !report.after_activation.same_chat_instance || !report.after_activation.same_owner ||
          !report.after_activation.global_dirty || !report.after_activation.draft_retained ||
          report.after_activation.busy || report.after_activation.running || report.after_activation.controller_changes < 1) {
        throw new Error('dirty_state_not_retained_after_controllerchange');
      }

      report.stage = 'clear_draft'; persist();
      const clearPredicate = item => item.type === 'dirty_state_changed' && item.global_dirty === false &&
        Date.parse(item.at) > Date.parse(report.controller_event.at);
      const inspectChatInput = async () => page.evaluate(async () => {
        const chat = document.querySelector('habab-setup-chat');
        const field = chat?.shadowRoot?.querySelector('textarea');
        const text = field?.value || '';
        const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
        const draft_sha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
        const otherDirtyTags = [];
        const roots = [document];
        let seen = 0;
        while (roots.length && seen < 5000) {
          const root = roots.pop();
          for (const element of root.querySelectorAll('*')) {
            seen += 1;
            if (element !== chat) {
              try {
                if (element.isDirtyState === true || element.dirty === true) otherDirtyTags.push(element.localName);
              } catch (_) {}
            }
            if (element.shadowRoot) roots.push(element.shadowRoot);
            if (seen >= 5000) break;
          }
        }
        return {draft_length: text.length, draft_sha256,
          selection_start: field?.selectionStart ?? null, selection_end: field?.selectionEnd ?? null,
          field_focused: !!field && field.matches(':focus'),
          shadow_active_tag: chat?.shadowRoot?.activeElement?.localName || null,
          document_active_tag: document.activeElement?.localName || null,
          chat_busy: !!chat?.busy, voice_busy: !!chat?.voiceBusy, active_voice: chat?.voice !== undefined,
          pending_voice_text_length: typeof chat?.pendingVoiceText === 'string' ? chat.pendingVoiceText.length : 0,
          global_dirty: window.isDirtyState === true, other_dirty_provider_count: otherDirtyTags.length,
          other_dirty_provider_tags: otherDirtyTags.slice(0, 20)};
      });
      report.before_clear = await inspectChatInput();
      try {
        const focusState = await page.evaluate(() => {
          const chat = document.querySelector('habab-setup-chat');
          const field = chat?.shadowRoot?.querySelector('textarea');
          field?.focus({preventScroll: true});
          return {field_focused: !!field && field.matches(':focus'),
            shadow_active_tag: chat?.shadowRoot?.activeElement?.localName || null};
        });
        report.clear_focus = focusState;
        if (!focusState.field_focused || focusState.shadow_active_tag !== 'textarea') {
          throw new Error('chat_textarea_focus_not_confirmed');
        }
        await clearFocusedTextareaWithKeyboard(page.keyboard);
        report.clear_keyboard_completed = true;
        await delay(250);
        report.after_clear_input = await inspectChatInput();
      } catch (error) {
        report.clear_action_error = {at: now(), name: error?.name || 'Error',
          message: String(error?.message || '').slice(0, 300)};
        persist();
        throw error;
      }
      report.clear_keyboard_events = report.events.filter(item => item.type === 'chat_textarea_input' &&
        Date.parse(item.at) > Date.parse(report.controller_event.at)).map(item => ({
        at: item.at, is_trusted: item.is_trusted === true, draft_length: item.draft_length,
        selection_start: item.selection_start, selection_end: item.selection_end,
      }));
      if (report.after_clear_input.draft_length !== 0) throw new Error('trusted_keyboard_clear_failed');
      const chatOwnFlagsClear = report.after_clear_input.chat_busy === false && report.after_clear_input.voice_busy === false &&
        report.after_clear_input.active_voice === false && report.after_clear_input.pending_voice_text_length === 0;
      if (!chatOwnFlagsClear && report.after_clear_input.global_dirty) throw new Error('chat_provider_flags_remain_dirty');
      if (report.after_clear_input.global_dirty && report.after_clear_input.other_dirty_provider_count > 0) {
        throw new Error('global_dirty_held_by_other_provider');
      }
      const cleanEvent = waitEvent(clearPredicate, 8000, 'global_dirty_did_not_clear');
      const cleanChange = await cleanEvent;
      report.after_clear = {...report.after_clear_input, dirty_event_at: cleanChange.at,
        global_clean: cleanChange.global_dirty === false, chat_own_flags_clear: chatOwnFlagsClear};
      report.clean_at = cleanChange.at;
      report.dirty_phase_navigation_count = report.main_frame_documents.filter(item =>
        Date.parse(item.at) >= Date.parse(report.draft_set_at) && Date.parse(item.at) < Date.parse(report.clean_at)).length;
      const navAfterClean = await waitEvent(item => item.type === 'main_document_navigation' &&
        Date.parse(item.at) >= Date.parse(report.clean_at), 75000, 'deferred_reload_after_clean_timeout');
      report.deferred_reload = {at: navAfterClean.at, path: navAfterClean.path, loader_id: navAfterClean.loader_id || null,
        occurred_after_global_clean: Date.parse(navAfterClean.at) >= Date.parse(report.clean_at)};
      report.stage = 'post_reload_owner_session'; persist();
      await page.waitForFunction(expected => {
        const chat = document.querySelector('habab-setup-chat');
        const field = chat?.shadowRoot?.querySelector('textarea');
        return location.pathname === '/home/overview' && chat?.hass?.user?.is_admin === true && chat.sessionId === expected.session_id &&
          chat.hass.user.id === expected.owner_id && typeof window.haRegisterDirtyStateProvider === 'function' &&
          typeof chat.reloadStateProvider?.setDirty === 'function' && window.isDirtyState === false && field?.value === '' &&
          !chat.busy && !chat.running;
      }, {timeout: 60000, polling: 250}, {session_id: report.preflight.session_id, owner_id: report.preflight.owner_id});
      report.after_reload = await page.evaluate(async expected => {
        const chat = document.querySelector('habab-setup-chat');
        const [result, diagnostics] = await Promise.all([
          Promise.resolve({messages: Array.isArray(chat.messages) ? chat.messages.length : null,
            tail: (() => { const m=chat.messages?.at(-1); return m ? `${m.role}:${String(m.text || '').length}` : null; })()}),
          chat.hass.callWS({type: 'habab_home/agent/diagnostics', session_id: chat.sessionId}),
        ]);
        return {marker: window.__reloadGuardDocumentMarker, path: location.pathname,
          owner_id: chat.hass.user?.id || null, session_id: chat.sessionId,
          provider_api_ready: typeof window.haRegisterDirtyStateProvider === 'function' && typeof chat.reloadStateProvider?.setDirty === 'function',
          global_dirty: window.isDirtyState === true, draft_empty: chat.shadowRoot.querySelector('textarea')?.value === '',
          busy: !!chat.busy, running: !!chat.running, history_count: result.messages, last_message_signature: result.tail,
          diagnostics_active: diagnostics.active, expected_session: chat.sessionId === expected.session_id,
          expected_owner: chat.hass.user?.id === expected.owner_id};
      }, {session_id: report.preflight.session_id, owner_id: report.preflight.owner_id});
      report.after_reload.same_session = report.after_reload.expected_session;
      report.after_reload.same_owner = report.after_reload.expected_owner;
      report.after_reload.new_document = report.after_reload.marker !== report.preflight.marker;
      report.after_reload.history_preserved = report.after_reload.history_count === report.preflight.history_count;
      report.call_service_count = observer.callServiceCount;
      report.model_run_frame_count = report.model_run_frame_count || 0;
      report.forbidden_write_frame_count = report.forbidden_write_frame_count || 0;
      if (report.call_service_count !== 0 || report.model_run_frame_count !== 0 || report.forbidden_write_frame_count !== 0 ||
          report.after_reload.diagnostics_active !== false || !report.after_reload.same_owner || !report.after_reload.same_session ||
          !report.after_reload.draft_empty || report.after_reload.global_dirty || !report.after_reload.provider_api_ready ||
          !report.after_reload.new_document || !report.deferred_reload.occurred_after_global_clean) {
        throw new Error('post_reload_invariants_failed');
      }
      report.status = 'PASS';
      report.stage = 'complete';
  });
}

async function finalize() {
  if (finalized) return;
  finalized = true;
  if (report.status === 'IN_PROGRESS') report.status = 'FAIL';
  report.finished_at = now();
  report.call_service_count = activeObserver?.callServiceCount ?? null;
  if (activeObserver) {
    try { await activeObserver.unsubscribe(); } catch (_) {}
    activeObserver.close();
    activeObserver = null;
  }
  const summary = publicSummary();
  report.public_summary = summary;
  persist();
  fs.writeFileSync(PUBLIC_PATH, JSON.stringify(summary, null, 2) + '\n', {mode: 0o644});
  fs.chmodSync(PUBLIC_PATH, 0o644);
  process.stdout.write(JSON.stringify({attempt: report.attempt, status: summary.status, stage: report.stage,
    runtime_identity_verified: report.runtime_identity_verified === true,
    provider_api_ready: report.provider_api_ready === true,
    controller_change_count: report.controller_changes.length,
    reloads_while_dirty: report.dirty_phase_navigation_count ?? null,
    draft_retained_after_activation: report.after_activation?.draft_retained ?? null,
      after_clear_draft_length: report.after_clear_input?.draft_length ?? null,
      after_clear_global_dirty: report.after_clear_input?.global_dirty ?? null,
      other_dirty_provider_count: report.after_clear_input?.other_dirty_provider_count ?? null,
      trusted_clear_input_events: report.clear_keyboard_events?.filter(item => item.is_trusted).length ?? 0,
    clean_before_deferred_reload: report.after_clear?.global_clean ?? null,
    same_session_after_reload: report.after_reload?.same_session ?? null,
    model_run_frames: report.model_run_frame_count ?? 0,
    forbidden_write_frames: report.forbidden_write_frame_count ?? 0,
    call_service_events: report.call_service_count ?? null,
    failure_code: report.failure_code || null}) + '\n');
}

main().catch(error => {
  const code = report.stage === 'wait_pre_activation_window' && error?.name === 'TimeoutError'
    ? 'fresh_worker_install_window_unavailable'
    : report.stage === 'chat_provider_ready' && error?.name === 'TimeoutError'
      ? 'chat_provider_bootstrap_timeout'
    : /^[a-z0-9_]+$/.test(error?.message || '') ? error.message : 'reload_guard_qa_failed';
  report.status = ['fresh_worker_install_window_unavailable', 'first_sw_activation_preceded_draft'].includes(code)
    ? 'PRECONDITION_NOT_MET' : 'FAIL';
  report.failure_code = code;
  report.failure_class = error?.name || 'Error';
  process.exitCode = 1;
}).then(finalize, async () => finalize()).finally(() => {
  for (const waiter of waiters) { clearTimeout(waiter.timer); waiter.reject(new Error('runner_terminated')); }
  waiters.clear();
});

