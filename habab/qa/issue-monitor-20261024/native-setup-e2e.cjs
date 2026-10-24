'use strict';

// Live run is intentionally fail-closed until the owner supplies the exact GO.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {execFileSync, spawnSync} = require('node:child_process');
const puppeteer = require('/root/.nvm/versions/node/v22.21.1/lib/node_modules/puppeteer');
const {withOwnedChromium} = require('/root/tools/chrome-daemon/owned-browser.cjs');

const STAGE_TIMEOUT = Object.freeze({
  connection: 90000,
  idle: 45000,
  firstTurn: 150000,
  form: 45000,
  submit: 90000,
  secondTurn: 150000,
  cleanup: 60000,
});
const TASK_TIMEOUT_MS = 900000;
const FIXTURE_PREFIX = 'habab_qa_local_todo';
const STORE_DIR = '/config/.storage';
const MANIFEST = path.resolve(__dirname, '../../distribution/src/habab_home/manifest.json');
const ALLOWED_REMOTE_HOST = 'habab-demo.wpmix.net';
const LOCAL_HOSTS = new Set(['127.0.0.1', 'localhost', '::1']);
const ERROR_CODE_RE = /^[a-z0-9_]{1,80}$/;

function fail(code) {
  const error = new Error(code);
  error.code = code;
  return error;
}

function safeErrorName(error) {
  return typeof error?.name === 'string' && /^[A-Za-z0-9_]{1,40}$/.test(error.name)
    ? error.name : 'Error';
}

function safeFailureCode(error, stage) {
  if (ERROR_CODE_RE.test(error?.code || '')) return error.code;
  const safeStage = /^[a-z0-9_]{1,60}$/.test(stage) ? stage : 'runner';
  return safeErrorName(error) === 'TimeoutError' ? `${safeStage}_timeout` : `${safeStage}_failed`;
}

function requireEnv(name, pattern) {
  const value = process.env[name];
  if (!value || (pattern && !pattern.test(value))) throw fail(`missing_or_invalid_${name.toLowerCase()}`);
  return value;
}

function getConfig() {
  if (process.env.HABAB_QA_GO !== '1') throw fail('NOT_RUN_requires_explicit_HABAB_QA_GO_1');
  const base = new URL(requireEnv('HABAB_QA_BASE_URL'));
  const isLocal = LOCAL_HOSTS.has(base.hostname);
  if ((!isLocal && (base.hostname !== ALLOWED_REMOTE_HOST || base.protocol !== 'https:')) ||
      (isLocal && base.protocol !== 'http:' && base.protocol !== 'https:') ||
      base.pathname !== '/' || base.search || base.hash || base.username || base.password) {
    throw fail('target_not_allowlisted_origin');
  }
  const baseUrl = base.origin;
  const container = requireEnv('HABAB_QA_CONTAINER', /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/);
  const expectedImage = requireEnv('HABAB_QA_EXPECTED_IMAGE_ID', /^sha256:[a-f0-9]{64}$/);
  const expectedSource = requireEnv('HABAB_QA_EXPECTED_SOURCE', /^[a-f0-9]{40}$/);
  const expectedProduct = requireEnv('HABAB_QA_EXPECTED_PRODUCT', /^\d+\.\d+\.\d+$/);
  const expectedCore = requireEnv('HABAB_QA_EXPECTED_CORE', /^\d{4}\.\d{1,2}\.\d{1,2}$/);
  const executionWindowId = requireEnv('HABAB_QA_EXECUTION_WINDOW_ID', /^[A-Za-z0-9_.:-]{1,80}$/);
  const authPath = path.resolve(requireEnv('HABAB_QA_AUTH_FILE'));
  const reportPath = path.resolve(requireEnv('HABAB_QA_REPORT'));
  const privateTracePath = path.resolve(requireEnv('HABAB_QA_PRIVATE_TRACE'));
  if (fs.existsSync(reportPath) || fs.existsSync(privateTracePath)) throw fail('qa_output_path_preexists');
  if (!privateTracePath.startsWith('/root/.local/share/habab-owner-test/')) {
    throw fail('private_trace_must_stay_in_owner_test_storage');
  }
  if (reportPath === privateTracePath) throw fail('report_and_private_trace_must_differ');
  return {baseUrl, container, expectedImage, expectedSource, expectedProduct, expectedCore,
    executionWindowId, authPath, reportPath, privateTracePath};
}

function dockerIdentity(config) {
  const template = '{{.Image}}|{{.State.Running}}|{{index .Config.Labels "org.opencontainers.image.revision"}}|' +
    '{{index .Config.Labels "org.opencontainers.image.version"}}|{{index .Config.Labels "io.hass.version"}}';
  const value = execFileSync('docker', ['inspect', '--format', template, config.container], {
    encoding: 'utf8', timeout: 10000, stdio: ['ignore', 'pipe', 'ignore'],
  }).trim().split('|');
  if (value.length !== 5) throw fail('container_identity_unavailable');
  const [imageId, running, source, product, core] = value;
  if (imageId !== config.expectedImage || running !== 'true' || source !== config.expectedSource ||
      product !== config.expectedProduct || core !== config.expectedCore) throw fail('container_release_identity_mismatch');
  const manifest = JSON.parse(fs.readFileSync(MANIFEST, 'utf8'));
  if (manifest.version !== config.expectedProduct) throw fail('local_manifest_product_mismatch');
  return {imageId, source, product, core};
}

function dockerStoreExists(config, fixtureKey) {
  if (!/^[a-z0-9_]{1,180}$/.test(fixtureKey)) throw fail('fixture_storage_key_invalid');
  const storePath = `${STORE_DIR}/local_todo.${fixtureKey}.ics`;
  const result = spawnSync('docker', ['exec', config.container, 'test', '-e', storePath], {
    encoding: 'utf8', timeout: 7000, stdio: ['ignore', 'ignore', 'ignore'],
  });
  if (result.error || (result.status !== 0 && result.status !== 1)) throw fail('fixture_store_probe_failed');
  return result.status === 0;
}

function dockerStoreAbsent(config, fixtureKey) {
  return !dockerStoreExists(config, fixtureKey);
}

function dockerFixtureStorageKey(config, entryId, fixtureName) {
  const script = [
    'import json, sys',
    'from pathlib import Path',
    'path = Path("/config/.storage/core.config_entries")',
    'store = json.loads(path.read_text(encoding="utf-8"))',
    'entries = store.get("data", {}).get("entries", [])',
    'matches = [e for e in entries if e.get("domain") == "local_todo" and e.get("entry_id") == sys.argv[1] and e.get("title") == sys.argv[2]]',
    'if len(matches) != 1:',
    '    print(json.dumps({"count": len(matches)}))',
    'else:',
    '    data = matches[0].get("data")',
    '    key = data.get("storage_key") if isinstance(data, dict) else None',
    '    print(json.dumps({"count": 1, "entry_id": matches[0].get("entry_id"), "title": matches[0].get("title"), "storage_key": key}))',
  ].join('\n');
  try {
    const output = execFileSync('docker', ['exec', config.container, 'python', '-c', script, entryId, fixtureName], {
      encoding: 'utf8', timeout: 3000, stdio: ['ignore', 'pipe', 'ignore'], maxBuffer: 2048,
    });
    const result = JSON.parse(output);
    if (result.count !== 1 || result.entry_id !== entryId || result.title !== fixtureName ||
        typeof result.storage_key !== 'string' || !/^[a-z0-9_]{1,180}$/.test(result.storage_key)) {
      throw fail('exact_fixture_storage_key_unavailable');
    }
    return result.storage_key;
  } catch (error) {
    if (ERROR_CODE_RE.test(error?.code || '')) throw error;
    throw fail('exact_fixture_storage_read_failed');
  }
}

async function waitForFixtureStorageKey(config, entryId, fixtureName) {
  for (let attempt = 0; attempt < 6; attempt++) {
    try {
      return dockerFixtureStorageKey(config, entryId, fixtureName);
    } catch (error) {
      if (error?.code !== 'exact_fixture_storage_key_unavailable' &&
          error?.code !== 'exact_fixture_storage_read_failed') throw error;
      if (attempt === 5) break;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
  throw fail('exact_fixture_storage_persist_timeout');
}

function loadFreshAuth(config) {
  const stat = fs.statSync(config.authPath);
  if (!stat.isFile() || (stat.mode & 0o077) !== 0) throw fail('auth_file_permissions_invalid');
  const auth = JSON.parse(fs.readFileSync(config.authPath, 'utf8'));
  if (typeof auth.access_token !== 'string' || !auth.access_token ||
      typeof auth.expires_in !== 'number' || auth.expires_in <= 0 ||
      typeof auth.client_id !== 'string' || new URL(auth.client_id).origin !== config.baseUrl) {
    throw fail('auth_file_target_mismatch');
  }
  return {access_token: auth.access_token, expires_in: auth.expires_in,
    token_type: typeof auth.token_type === 'string' ? auth.token_type : 'Bearer',
    hassUrl: config.baseUrl, clientId: auth.client_id,
    expires: Date.now() + auth.expires_in * 1000};
}

async function readHaSnapshot(page, fixtureName, baselineIds) {
  return page.evaluate(async ({name, ids}) => {
    const find = window.__hababQaFind;
    const chat = find('habab-setup-chat');
    if (!chat?.hass?.callWS) throw new Error('ha_connection_missing');
    const [entries, registry, states] = await Promise.all([
      chat.hass.callWS({type: 'config_entries/get'}),
      chat.hass.callWS({type: 'config/entity_registry/list'}),
      chat.hass.callWS({type: 'get_states'}),
    ]);
    const entryList = Array.isArray(entries) ? entries : [];
    const registryList = Array.isArray(registry) ? registry : [];
    const stateList = Array.isArray(states) ? states : [];
    const exact = entryList.filter((entry) => entry.domain === 'local_todo' && entry.title === name);
    const baseline = new Set(ids);
    const fresh = exact.filter((entry) => !baseline.has(entry.entry_id));
    const matching = fresh.length === 1 ? fresh[0] : null;
    const entities = matching ? registryList.filter((entry) => entry.config_entry_id === matching.entry_id) : [];
    const todoEntities = entities.filter((entry) => entry.entity_id.startsWith('todo.'));
    const entityState = todoEntities.length === 1
      ? stateList.find((state) => state.entity_id === todoEntities[0].entity_id)?.state
      : undefined;
    const chatState = {
      sessionId: chat.sessionId,
      busy: Boolean(chat.busy),
      opened: Boolean(chat.shadowRoot?.querySelector('dialog')?.open),
      admin: chat.hass.user?.is_admin === true,
      core: chat.hass.config?.version,
      localTodoEntries: entryList.filter((entry) => entry.domain === 'local_todo').length,
      fixtureExactCount: exact.length,
      newEntryCount: fresh.length,
      entryId: matching?.entry_id,
      entryDomain: matching?.domain,
      entryTitleMatches: matching?.title === name,
      entryState: matching?.state,
      ownedEntityCount: entities.length,
      todoEntityCount: todoEntities.length,
      ownedEntityIds: entities.map((entry) => entry.entity_id),
      todoEntityIds: todoEntities.map((entry) => entry.entity_id),
      todoState: entityState,
    };
    return chatState;
  }, {name: fixtureName, ids: baselineIds});
}

async function readSelectedProvider(page) {
  return page.evaluate(async () => {
    const find = window.__hababQaFind;
    const chat = find('habab-setup-chat');
    const result = await chat.hass.callWS({type: 'habab_home/providers/list'});
    const connections = Array.isArray(result?.connections) ? result.connections : [];
    const selected = connections.filter((connection) => connection.assistant_selected === true);
    return {selectedCount: selected.length, providers: selected.map((entry) => entry.provider)};
  });
}

async function findFixtureCard(page) {
  return page.evaluate(() => {
    const find = window.__hababQaFind;
    const chat = find('habab-setup-chat');
    const setup = chat?.integrationCard;
    const cards = setup?.cards ? [...setup.cards.values()] : [];
    const localCards = cards.filter((card) => card.record?.domain === 'local_todo');
    if (localCards.length !== 1) return {count: localCards.length};
    const card = localCards[0];
    const schema = Array.isArray(card.record.data_schema) ? card.record.data_schema : [];
    const field = schema.find((entry) => entry.name === 'todo_list_name');
    return {
      count: 1,
      phase: card.record.phase,
      kind: card.record.kind,
      step: card.record.step_id,
      fieldNames: schema.map((entry) => entry.name),
      fieldRequired: field?.required === true,
      nativeForm: card.form?.localName,
      nativeContext: card.context?.localName,
      nativeStepFields: Array.isArray(card.form?.step?.data_schema)
        ? card.form.step.data_schema.map((entry) => entry.name)
        : [],
      formReady: Boolean(card.form && card.readyForm === card.form && card.form.isConnected),
      submitEnabled: Boolean(card.submitButton && !card.submitButton.disabled),
      interactionId: card.id,
      entryState: card.record.entry_state,
      entityCount: card.record.entity_count,
    };
  });
}

async function sendChatText(page, text) {
  const send = await page.evaluateHandle(async (value) => {
    const find = window.__hababQaFind;
    const chat = find('habab-setup-chat');
    const textarea = chat?.shadowRoot?.querySelector('textarea');
    const button = chat?.shadowRoot?.querySelector('.send');
    if (!chat || !textarea || !button || button.disabled || textarea.disabled) {
      window.__hababQaSendTelemetry = {visible: false, reason: 'send_control_unavailable'};
      return null;
    }
    const messages = Array.isArray(chat.messages) ? chat.messages : [];
    window.__hababQaSendBaseline = {
      sessionId: chat.sessionId,
      lastUser: [...messages].reverse().find((message) => message.role === 'user'),
    };
    textarea.focus();
    textarea.value = value;
    textarea.dispatchEvent(new InputEvent('input', {bubbles: true, composed: true, inputType: 'insertText', data: value}));
    const attempt = {chat, textarea, button, sessionId: chat.sessionId, draft: value};
    window.__hababQaSendAttempt = attempt;
    const deadline = performance.now() + 5000;
    const nextFrame = () => new Promise((resolve) => {
      const remaining = deadline - performance.now();
      if (remaining <= 0) return resolve(false);
      let timeout;
      const frame = requestAnimationFrame(() => {
        clearTimeout(timeout);
        resolve(true);
      });
      timeout = setTimeout(() => {
        cancelAnimationFrame(frame);
        resolve(false);
      }, remaining);
    });
    const composedContains = (parent, child) => {
      let node = child;
      while (node) {
        if (node === parent || parent.contains(node)) return true;
        const root = node.getRootNode?.();
        node = node.parentNode || root?.host || null;
      }
      return false;
    };
    const composedHit = (x, y) => {
      let hit = document.elementFromPoint(x, y);
      while (hit?.shadowRoot?.elementFromPoint) {
        const nested = hit.shadowRoot.elementFromPoint(x, y);
        if (!nested || nested === hit) break;
        hit = nested;
      }
      return hit;
    };
    let previousRect;
    let lastTelemetry = {visible: false, stable: false, reason: 'layout_pending'};
    while (performance.now() < deadline) {
      if (find('habab-setup-chat') !== attempt.chat || attempt.chat.sessionId !== attempt.sessionId) {
        lastTelemetry = {visible: false, stable: false, reason: 'chat_node_or_session_replaced'};
        break;
      }
      if (attempt.chat.shadowRoot?.querySelector('textarea') !== attempt.textarea ||
          attempt.chat.shadowRoot?.querySelector('.send') !== attempt.button) {
        lastTelemetry = {visible: false, stable: false, reason: 'send_control_replaced'};
        break;
      }
      if (attempt.textarea.value !== attempt.draft) {
        lastTelemetry = {visible: false, stable: false, reason: 'draft_changed'};
        break;
      }
      if (!await nextFrame()) break;
      const rect = attempt.button.getBoundingClientRect();
      const stable = Boolean(previousRect && Math.abs(previousRect.x - rect.x) <= 1 &&
        Math.abs(previousRect.y - rect.y) <= 1 && Math.abs(previousRect.width - rect.width) <= 1 &&
        Math.abs(previousRect.height - rect.height) <= 1);
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      const hit = composedHit(x, y);
      const visible = stable && rect.width > 0 && rect.height > 0 && !attempt.button.disabled &&
        !attempt.button.hidden && Boolean(hit && composedContains(attempt.button, hit));
      lastTelemetry = {visible, stable, reason: visible ? null : stable ? 'hit_test_pending' : 'layout_pending',
        rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
        hit_local_name: hit?.localName || null, pointerdown: false, pointerup: false, click: false};
      if (visible) break;
      previousRect = rect;
      const remaining = deadline - performance.now();
      if (remaining > 0) await new Promise((resolve) => setTimeout(resolve, Math.min(100, remaining)));
    }
    if (!lastTelemetry.visible && !['chat_node_or_session_replaced', 'send_control_replaced', 'draft_changed'].includes(lastTelemetry.reason)) {
      lastTelemetry.reason = 'hit_test_readiness_timeout';
    }
    window.__hababQaSendTelemetry = lastTelemetry;
    if (!lastTelemetry.visible) return null;
    attempt.button.addEventListener('pointerdown', () => { window.__hababQaSendTelemetry.pointerdown = true; }, {once: true});
    attempt.button.addEventListener('pointerup', () => { window.__hababQaSendTelemetry.pointerup = true; }, {once: true});
    attempt.button.addEventListener('click', () => { window.__hababQaSendTelemetry.click = true; }, {once: true});
    return attempt.button;
  }, text);
  try {
    const element = send.asElement();
    if (element) await element.click();
    return page.evaluate(() => window.__hababQaSendTelemetry || {visible: false, reason: 'send_telemetry_unavailable'});
  } finally {
    await send.dispose();
  }
}

function requireTrustedVisibleSend(pointer) {
  if (!pointer.visible) {
    const reason = /^[a-z0-9_]{1,60}$/.test(pointer.reason || '') ? pointer.reason : 'button_not_visible';
    throw fail(`chat_send_readiness_${reason}`);
  }
  if (!pointer.stable) throw fail('chat_send_button_rect_unstable');
  if (!pointer.pointerdown || !pointer.pointerup || !pointer.click) {
    throw fail('chat_send_pointer_dispatch_unconfirmed');
  }
}

async function verifyChatSendAccepted(page, sessionId, prompt) {
  const accepted = await page.waitForFunction(({expectedSession, expectedPrompt}) => {
    const chat = window.__hababQaFind('habab-setup-chat');
    const baseline = window.__hababQaSendBaseline;
    if (!chat || chat.sessionId !== expectedSession || baseline?.sessionId !== expectedSession) return false;
    const messages = Array.isArray(chat.messages) ? chat.messages : [];
    const latestUser = [...messages].reverse().find((message) => message.role === 'user');
    if (!latestUser || latestUser === baseline.lastUser || latestUser.text !== expectedPrompt) return false;
    if (chat.busy) return 'busy_with_new_user_message';
    const userIndex = messages.lastIndexOf(latestUser);
    return messages.slice(userIndex + 1).some((message) => message.role === 'assistant')
      ? 'assistant_response_after_new_user_message' : false;
  }, {timeout: 5000, polling: 100}, {expectedSession: sessionId, expectedPrompt: prompt});
  try {
    return await accepted.jsonValue();
  } finally {
    await accepted.dispose();
  }
}

async function inspectChatTurn(page, sessionId, prompt) {
  return page.evaluate(({expectedSession, expectedPrompt}) => {
    const chat = window.__hababQaFind('habab-setup-chat');
    const messages = Array.isArray(chat?.messages) ? chat.messages : [];
    const latestUser = [...messages].reverse().find((message) => message.role === 'user');
    return {session_matches: chat?.sessionId === expectedSession,
      latest_user_matches: latestUser?.text === expectedPrompt,
      busy: Boolean(chat?.busy)};
  }, {expectedSession: sessionId, expectedPrompt: prompt});
}

async function waitForLastAssistant(page, text, timeout) {
  await page.waitForFunction((expected) => {
    const find = window.__hababQaFind;
    const chat = find('habab-setup-chat');
    if (!chat || chat.busy || !chat.sessionId) return false;
    const messages = Array.isArray(chat.messages) ? chat.messages : [];
    let userIndex = -1;
    let assistantIndex = -1;
    for (let index = messages.length - 1; index >= 0; index--) {
      const message = messages[index];
      if (userIndex < 0 && message.role === 'user' && message.text === expected) userIndex = index;
      if (assistantIndex < 0 && message.role === 'assistant') assistantIndex = index;
      if (userIndex >= 0 && assistantIndex >= 0) break;
    }
    return userIndex >= 0 && assistantIndex > userIndex &&
      typeof messages[assistantIndex]?.text === 'string' && messages[assistantIndex].text.trim().length > 0;
  }, {timeout, polling: 500}, text);
}

async function waitForNativeForm(page, expectedSession) {
  await page.waitForFunction((sessionId) => {
    const find = window.__hababQaFind;
    const chat = find('habab-setup-chat');
    if (!chat || chat.busy || chat.sessionId !== sessionId) return false;
    const interactions = chat.integrationCard?.interactions || [];
    const userIndex = [...(chat.messages || [])].map((message) => message.role === 'user' &&
      message.text.startsWith('Добавь ровно одну интеграцию Local To-do')).lastIndexOf(true);
    const assistantAfterPrompt = userIndex >= 0 && [...chat.messages].slice(userIndex + 1)
      .some((message) => message.role === 'assistant');
    const cards = chat.integrationCard?.cards ? [...chat.integrationCard.cards.values()] : [];
    const forms = cards.filter((card) => card.record?.domain === 'local_todo' &&
      card.record.phase === 'form' && card.form && card.readyForm === card.form &&
      card.submitButton && !card.submitButton.disabled);
    return assistantAfterPrompt && interactions.some((item) => item.domain === 'local_todo' && item.phase === 'form') && forms.length === 1;
  }, {timeout: STAGE_TIMEOUT.firstTurn, polling: 500}, expectedSession);
}

async function nativeFieldHandle(page, interactionId, fieldName) {
  return page.evaluateHandle(({id, name}) => {
    const find = window.__hababQaFind;
    const chat = find('habab-setup-chat');
    const card = [...(chat?.integrationCard?.cards?.values?.() || [])]
      .find((item) => item.id === id && item.record?.domain === 'local_todo');
    if (!card?.form || card.record.phase !== 'form') return null;
    const candidates = [];
    const walk = (root) => {
      for (const element of root.querySelectorAll('*')) {
        if (element instanceof HTMLInputElement && element.type === 'text' &&
            element.getBoundingClientRect().width > 0 && element.getBoundingClientRect().height > 0 &&
            !element.disabled && !element.readOnly) candidates.push(element);
        if (element.shadowRoot) walk(element.shadowRoot);
      }
    };
    walk(card.form.shadowRoot || card.form);
    const named = candidates.filter((input) => input.name === name || input.id === name ||
      input.getAttribute('data-name') === name || input.getAttribute('name') === name);
    return named.length === 1 ? named[0] : candidates.length === 1 ? candidates[0] : null;
  }, {id: interactionId, name: fieldName});
}

async function readChatRunState(page, sessionId, prompt) {
  return page.evaluate(({expectedSession, expectedPrompt}) => {
    const find = window.__hababQaFind;
    const chat = find('habab-setup-chat');
    const messages = Array.isArray(chat?.messages) ? chat.messages : [];
    let userIndex = -1;
    let assistantIndex = -1;
    for (let index = messages.length - 1; index >= 0; index--) {
      if (userIndex < 0 && messages[index].role === 'user' && messages[index].text === expectedPrompt) userIndex = index;
      if (assistantIndex < 0 && messages[index].role === 'assistant') assistantIndex = index;
      if (userIndex >= 0 && assistantIndex >= 0) break;
    }
    return {
      sameSession: chat?.sessionId === expectedSession,
      idle: chat?.busy === false,
      assistantAfterPrompt: userIndex >= 0 && assistantIndex > userIndex,
      assistantResponsePresent: assistantIndex >= 0 && Boolean(messages[assistantIndex]?.text?.trim()),
      chatOpen: Boolean(chat?.shadowRoot?.querySelector('dialog')?.open),
      statusEmpty: chat?.shadowRoot?.querySelector('#status')?.textContent.trim() === '',
    };
  }, {expectedSession: sessionId, expectedPrompt: prompt});
}

async function stopOwnChat(page, sessionId, lastPrompt) {
  const state = await page.evaluate(({expectedSession, expectedPrompt}) => {
    const chat = window.__hababQaFind('habab-setup-chat');
    const messages = Array.isArray(chat?.messages) ? chat.messages : [];
    const latestUser = [...messages].reverse().find((message) => message.role === 'user');
    if (!chat || chat.sessionId !== expectedSession || latestUser?.text !== expectedPrompt) {
      return {ownsLatestTurn: false, busy: Boolean(chat?.busy)};
    }
    if (!chat.busy) return {ownsLatestTurn: true, busy: false};
    const stop = chat.shadowRoot?.querySelector('.stop');
    if (!stop || stop.hidden || stop.disabled) return {ownsLatestTurn: true, busy: true, stopAvailable: false};
    stop.click();
    return {ownsLatestTurn: true, busy: true, stopAvailable: true};
  }, {expectedSession: sessionId, expectedPrompt: lastPrompt});
  if (!state.ownsLatestTurn) return {ownershipConfirmed: false, idle: false, stopped: false};
  if (!state.busy) return {ownershipConfirmed: true, idle: true, stopped: false};
  if (!state.stopAvailable) return {ownershipConfirmed: true, idle: false, stopped: false};
  try {
    await page.waitForFunction(({expectedSession, expectedPrompt}) => {
      const chat = window.__hababQaFind('habab-setup-chat');
      const messages = Array.isArray(chat?.messages) ? chat.messages : [];
      const latestUser = [...messages].reverse().find((message) => message.role === 'user');
      return chat?.sessionId === expectedSession && latestUser?.text === expectedPrompt && !chat.busy;
    }, {timeout: 15000, polling: 250}, {expectedSession: sessionId, expectedPrompt: lastPrompt});
    return {ownershipConfirmed: true, idle: true, stopped: true};
  } catch {
    return {ownershipConfirmed: false, idle: false, stopped: true};
  }
}

async function clickCancelForOwnFlow(page, interactionId, sessionId, lastPrompt) {
  if (!interactionId) return false;
  return page.evaluate(({id, expectedSession, expectedPrompt}) => {
    const find = window.__hababQaFind;
    const chat = find('habab-setup-chat');
    const messages = Array.isArray(chat?.messages) ? chat.messages : [];
    const latestUser = [...messages].reverse().find((message) => message.role === 'user');
    if (!chat || chat.sessionId !== expectedSession || latestUser?.text !== expectedPrompt) return false;
    const card = [...(chat?.integrationCard?.cards?.values?.() || [])]
      .find((item) => item.id === id && item.record?.domain === 'local_todo');
    if (!card || !['form', 'menu', 'external', 'progress'].includes(card.record.phase)) return false;
    const button = [...card.actions.querySelectorAll('button')]
      .find((item) => ['Отмена', 'Cancel'].includes(item.textContent.trim()) && !item.disabled);
    if (!button) return false;
    button.click();
    return true;
  }, {id: interactionId, expectedSession: sessionId, expectedPrompt: lastPrompt});
}

async function removeExactEntry(page, config, entryId, fixtureName, fixtureKey, expectedFixtureKey,
    baselineIds, sessionId, lastPrompt) {
  const fragmentMatches = await page.evaluate(async ({id, name, oldIds, expectedSession, expectedPrompt}) => {
    const find = window.__hababQaFind;
    const chat = find('habab-setup-chat');
    if (!chat?.hass?.callWS) return {deleted: false, reason: 'ha_unavailable'};
    const ownsTurn = () => {
      const messages = Array.isArray(chat.messages) ? chat.messages : [];
      const latestUser = [...messages].reverse().find((message) => message.role === 'user');
      return chat.sessionId === expectedSession && latestUser?.text === expectedPrompt;
    };
    if (!ownsTurn()) return 0;
    const entries = await chat.hass.callWS({type: 'config_entries/get'});
    const baseline = new Set(oldIds);
    const matches = (Array.isArray(entries) ? entries : []).filter((entry) =>
      entry.domain === 'local_todo' && entry.entry_id === id && entry.title === name && !baseline.has(entry.entry_id)).length;
    return ownsTurn() ? matches : 0;
  }, {id: entryId, name: fixtureName, oldIds: baselineIds,
    expectedSession: sessionId, expectedPrompt: lastPrompt});
  if (fixtureKey !== expectedFixtureKey || fragmentMatches !== 1 ||
      await waitForFixtureStorageKey(config, entryId, fixtureName) !== expectedFixtureKey) {
    return {deleted: false, reason: 'exact_new_entry_not_unique'};
  }
  const deleted = await page.evaluate(async ({id, expectedSession, expectedPrompt}) => {
    const chat = window.__hababQaFind('habab-setup-chat');
    const messages = Array.isArray(chat?.messages) ? chat.messages : [];
    const latestUser = [...messages].reverse().find((message) => message.role === 'user');
    if (chat?.sessionId !== expectedSession || latestUser?.text !== expectedPrompt) return false;
    await chat.hass.callApi('DELETE', `config/config_entries/entry/${encodeURIComponent(id)}`);
    return true;
  }, {id: entryId, expectedSession: sessionId, expectedPrompt: lastPrompt});
  return {deleted};
}

async function verifyCleanup(page, entryId, fixtureName, baselineIds, ownedEntityIds) {
  return page.evaluate(async ({id, name, oldIds, entityIds}) => {
    const find = window.__hababQaFind;
    const chat = find('habab-setup-chat');
    const [entries, registry, states] = await Promise.all([
      chat.hass.callWS({type: 'config_entries/get'}),
      chat.hass.callWS({type: 'config/entity_registry/list'}),
      chat.hass.callWS({type: 'get_states'}),
    ]);
    const baseline = new Set(oldIds);
    const exactFresh = (Array.isArray(entries) ? entries : []).filter((entry) =>
      entry.domain === 'local_todo' && entry.entry_id === id && entry.title === name &&
      !baseline.has(entry.entry_id));
    const ownedIds = new Set(entityIds);
    const registryIds = (Array.isArray(registry) ? registry : [])
      .filter((entry) => ownedIds.has(entry.entity_id)).map((entry) => entry.entity_id);
    const stateIds = new Set((Array.isArray(states) ? states : []).map((item) => item.entity_id));
    return {
      exactFreshEntriesRemain: exactFresh.length,
      ownedEntityRegistryEntriesRemain: registryIds.length,
      ownedEntityStatesRemain: [...ownedIds].filter((id) => stateIds.has(id)).length,
    };
  }, {id: entryId, name: fixtureName, oldIds: baselineIds, entityIds: ownedEntityIds});
}

async function waitForExactCleanup(page, entryId, fixtureName, baselineIds, ownedEntityIds) {
  await page.waitForFunction(async ({id, name, oldIds, entityIds}) => {
    const find = window.__hababQaFind;
    const chat = find('habab-setup-chat');
    const [entries, registry, states] = await Promise.all([
      chat.hass.callWS({type: 'config_entries/get'}),
      chat.hass.callWS({type: 'config/entity_registry/list'}),
      chat.hass.callWS({type: 'get_states'}),
    ]);
    const baseline = new Set(oldIds);
    const entryRemain = (Array.isArray(entries) ? entries : []).some((entry) =>
      entry.domain === 'local_todo' && entry.entry_id === id && entry.title === name && !baseline.has(entry.entry_id));
    const ownedIds = new Set(entityIds);
    const registryRemain = (Array.isArray(registry) ? registry : []).some((entry) => ownedIds.has(entry.entity_id));
    const stateIds = new Set((Array.isArray(states) ? states : []).map((item) => item.entity_id));
    return !entryRemain && !registryRemain && ![...ownedIds].some((entityId) => stateIds.has(entityId));
  }, {timeout: STAGE_TIMEOUT.cleanup, polling: 500}, {
    id: entryId, name: fixtureName, oldIds: baselineIds, entityIds: ownedEntityIds,
  });
}

async function closeOwnInteractionCard(page, interactionId, sessionId, lastPrompt) {
  if (!interactionId) return false;
  return page.evaluate(({id, expectedSession, expectedPrompt}) => {
    const find = window.__hababQaFind;
    const chat = find('habab-setup-chat');
    const messages = Array.isArray(chat?.messages) ? chat.messages : [];
    const latestUser = [...messages].reverse().find((message) => message.role === 'user');
    if (!chat || chat.sessionId !== expectedSession || latestUser?.text !== expectedPrompt) return false;
    const card = [...(chat?.integrationCard?.cards?.values?.() || [])]
      .find((item) => item.id === id && item.record?.domain === 'local_todo');
    if (!card) return true;
    const close = [...card.actions.querySelectorAll('button')]
      .find((button) => ['Закрыть', 'Close'].includes(button.textContent.trim()) && !button.disabled);
    if (!close) return false;
    close.click();
    return true;
  }, {id: interactionId, expectedSession: sessionId, expectedPrompt: lastPrompt});
}

async function main() {
  let config;
  const report = {
    started_at: new Date().toISOString(),
    verdict: 'NOT_RUN',
    scope: 'one Local To-do config flow through authenticated web chat and visible native form',
    physical_devices: false,
    prompts_sent: 0,
    setup_api_start_called: false,
    setup_api_submit_called: false,
    cleanup: {attempted: false, complete: false},
  };
  const trace = {events: [], page_error_count: 0, failed_request_count: 0, http_error_count: 0,
    identifiers: {}, send_acceptance: null, turn_state_at_failure: null, source_error_name: null};
  let stage = 'preflight';
  let pageRef;
  let sessionId;
  let lastSubmittedPrompt;
  let flowInteractionId;
  let fixtureName;
  let fixtureKey;
  let expectedFixtureKey;
  let baselineEntryIds = [];
  let preflightComplete = false;
  let createdEntryId;
  let configEntryCreated = false;
  let ownedEntityIds = [];
  let owned;
  let privateTraceCreated = false;

  function persistPrivateTrace() {
    if (!config?.privateTracePath) return;
    const contents = {started_at: report.started_at, updated_at: new Date().toISOString(),
      finished_at: report.finished_at, stage,
      execution_window_id: config.executionWindowId, events: trace.events,
      identifiers: trace.identifiers, send_acceptance: trace.send_acceptance,
      turn_state_at_failure: trace.turn_state_at_failure, source_error_name: trace.source_error_name,
      page_error_count: trace.page_error_count, failed_request_count: trace.failed_request_count,
      http_error_count: trace.http_error_count};
    fs.mkdirSync(path.dirname(config.privateTracePath), {recursive: true, mode: 0o700});
    fs.writeFileSync(config.privateTracePath, `${JSON.stringify(contents, null, 2)}\n`, {
      encoding: 'utf8', mode: 0o600, flag: privateTraceCreated ? 'w' : 'wx',
    });
    fs.chmodSync(config.privateTracePath, 0o600);
    privateTraceCreated = true;
  }

  let failureHandled = false;
  async function handleFailure(error) {
    if (failureHandled) return;
    failureHandled = true;
    report.verdict = report.verdict === 'NOT_RUN' ? 'BLOCKED' : 'FAIL';
    const errorName = safeErrorName(error);
    const errorCode = safeFailureCode(error, stage);
    report.failure = {stage, code: errorCode, source_error_name: errorName};
    trace.source_error_name = errorName;
    trace.events.push({stage, action: 'failed', code: errorCode, error_name: errorName});
    if (pageRef && sessionId && lastSubmittedPrompt && !owned?.page?.isClosed?.()) {
      try {
        const pointer = await pageRef.evaluate(() => window.__hababQaSendTelemetry || null);
        if (trace.send_acceptance) trace.send_acceptance.pointer = pointer;
        trace.turn_state_at_failure = await inspectChatTurn(pageRef, sessionId, lastSubmittedPrompt);
      } catch { trace.turn_state_at_failure = {inspection: 'failed'}; }
    }
    try { persistPrivateTrace(); } catch { /* preserve failure and do not attempt unsafe cleanup */ }
    if (pageRef && config && fixtureName && preflightComplete) {
      if (report.prompts_sent === 0 || !sessionId || !lastSubmittedPrompt) {
        report.cleanup = {attempted: false, complete: false, reason: 'no_owned_chat_turn'};
        return;
      }
      try {
        const ownership = await stopOwnChat(pageRef, sessionId, lastSubmittedPrompt);
        if (!ownership.ownershipConfirmed || !ownership.idle) {
          report.cleanup = {attempted: false, complete: false, reason: 'chat_ownership_guard_failed'};
          return;
        }
        trace.events.push({stage, action: 'owned_chat_idle_before_cleanup'});
        trace.turn_state_at_failure = await inspectChatTurn(pageRef, sessionId, lastSubmittedPrompt);
        persistPrivateTrace();
      } catch {
        report.cleanup = {attempted: false, complete: false, reason: 'chat_ownership_guard_failed'};
        return;
      }
      try {
        const snapshot = await readHaSnapshot(pageRef, fixtureName, baselineEntryIds);
        if (snapshot.newEntryCount === 1 && snapshot.entryDomain === 'local_todo' &&
            snapshot.entryTitleMatches && !baselineEntryIds.includes(snapshot.entryId)) {
          createdEntryId = snapshot.entryId;
          configEntryCreated = true;
          trace.identifiers.config_entry_id = createdEntryId;
          ownedEntityIds = snapshot.ownedEntityIds;
          trace.identifiers.owned_entity_ids = ownedEntityIds;
          persistPrivateTrace();
          fixtureKey = await waitForFixtureStorageKey(config, createdEntryId, fixtureName);
          trace.identifiers.storage_key = fixtureKey;
          persistPrivateTrace();
        }
      } catch { /* exact entry could not be independently recovered; leave it for owner review */ }
      if (configEntryCreated && createdEntryId && owned?.page?.isClosed?.() !== true) {
        try {
          report.cleanup = {attempted: true};
          let verify = await verifyCleanup(pageRef, createdEntryId, fixtureName, baselineEntryIds, ownedEntityIds);
          let fileAbsent = dockerStoreAbsent(config, fixtureKey);
          if (verify.exactFreshEntriesRemain || verify.ownedEntityRegistryEntriesRemain ||
              verify.ownedEntityStatesRemain || !fileAbsent) {
            const result = await removeExactEntry(pageRef, config, createdEntryId, fixtureName, fixtureKey,
              expectedFixtureKey, baselineEntryIds, sessionId, lastSubmittedPrompt);
            if (!result.deleted) throw fail('exact_fixture_cleanup_rejected');
            await waitForExactCleanup(pageRef, createdEntryId, fixtureName, baselineEntryIds, ownedEntityIds);
            verify = await verifyCleanup(pageRef, createdEntryId, fixtureName, baselineEntryIds, ownedEntityIds);
            fileAbsent = dockerStoreAbsent(config, fixtureKey);
          }
          const interactionClosed = await closeOwnInteractionCard(pageRef, flowInteractionId, sessionId, lastSubmittedPrompt);
          report.cleanup = {attempted: true, exact_new_entry_deleted: true,
            config_entry_absent: verify.exactFreshEntriesRemain === 0,
            entity_registry_absent: verify.ownedEntityRegistryEntriesRemain === 0,
            entity_state_absent: verify.ownedEntityStatesRemain === 0,
            local_todo_storage_file_absent: fileAbsent, own_interaction_card_closed: interactionClosed,
            broader_entries_touched: 0,
            complete: verify.exactFreshEntriesRemain === 0 && verify.ownedEntityRegistryEntriesRemain === 0 &&
              verify.ownedEntityStatesRemain === 0 && fileAbsent && interactionClosed};
        } catch {
          report.cleanup = {attempted: true, complete: false, exact_target_only: true};
        }
      } else if (pageRef && owned?.page?.isClosed?.() !== true) {
        try {
          let ownInteractionId = flowInteractionId;
          const canceled = ownInteractionId ? await clickCancelForOwnFlow(
            pageRef, ownInteractionId, sessionId, lastSubmittedPrompt) : false;
          report.cleanup = {attempted: true, own_flow_cancelled: canceled,
            no_config_entry_created: !configEntryCreated,
            complete: canceled, reason: canceled ? undefined : 'own_flow_id_not_confirmed'};
        } catch { report.cleanup = {attempted: true, complete: false, no_entry_id_recovered: true}; }
      }
    }
  }
  try {
    config = getConfig();
    const identity = dockerIdentity(config);
    const token = loadFreshAuth(config);
    const nonce = crypto.randomBytes(16).toString('hex');
    fixtureName = `${FIXTURE_PREFIX}_${new Date().toISOString().replace(/[-:TZ.]/g, '').slice(0, 14)}_${nonce}`;
    // HA's homeassistant.util.slugify preserves this already-slugged ASCII key.
    fixtureKey = fixtureName;
    expectedFixtureKey = fixtureKey;
    report.target_origin = config.baseUrl;
    report.execution_window_id = config.executionWindowId;
    report.release = {image_id: identity.imageId, source_revision: identity.source,
      product_version: identity.product, core_version: identity.core};
    report.fixture = {domain: 'local_todo', unique_name_generated: true, storage_key_generated: true,
        storage_contract: 'local_todo.config_flow.LocalTodoConfigFlow.async_step_user -> CONF_STORAGE_KEY=slugify(todo_list_name); local_todo.__init__.STORAGE_PATH; core.config_entries.SAVE_DELAY=1'};
    report.fixture_store_absent_before = !dockerStoreExists(config, fixtureKey);
    if (!report.fixture_store_absent_before) throw fail('fixture_store_preexists');

    stage = 'browser';
    await withOwnedChromium(puppeteer, {taskTimeoutMs: TASK_TIMEOUT_MS}, async (browser) => {
      owned = browser;
      pageRef = browser.page;
      const page = browser.page;
      let callbackError;
      try {
        page.setDefaultTimeout(30000);
        page.on('pageerror', () => trace.page_error_count++);
        page.on('requestfailed', () => trace.failed_request_count++);
        page.on('response', (response) => {
          if (response.status() >= 400) trace.http_error_count++;
        });
        await page.evaluateOnNewDocument((tokens) => {
          window.__hababQaFind = (selector, root = document) => {
            const direct = root.querySelector(selector);
            if (direct) return direct;
            for (const element of root.querySelectorAll('*')) {
              if (element.shadowRoot) {
                const nested = window.__hababQaFind(selector, element.shadowRoot);
                if (nested) return nested;
              }
            }
            return null;
          };
          localStorage.setItem('hassTokens', JSON.stringify(tokens));
          localStorage.setItem('selectedLanguage', JSON.stringify('ru'));
        }, token);
        stage = 'home_assistant_connection';
        await page.goto(`${config.baseUrl}/owner-test`, {waitUntil: 'networkidle2', timeout: 45000});
        await page.goto(`${config.baseUrl}/home/overview`, {waitUntil: 'networkidle2', timeout: 45000});
        report.route_pathname = await page.evaluate(() => location.pathname);
        trace.route_pathname = report.route_pathname;
        persistPrivateTrace();
        await page.waitForFunction(() => {
          const find = window.__hababQaFind;
          const chat = find('habab-setup-chat');
          return chat?.hass?.user?.is_admin === true && chat.hass.config?.version === '2026.10.2';
        }, {timeout: STAGE_TIMEOUT.connection});
        const provider = await readSelectedProvider(page);
        if (provider.selectedCount !== 1) throw fail('assistant_provider_not_uniquely_selected');
        report.assistant_provider = provider.providers[0] || 'selected';

        stage = 'chat_session';
        const before = await page.evaluate(async () => {
          const find = window.__hababQaFind;
          const chat = find('habab-setup-chat');
          if (!chat) throw new Error('chat_missing');
          if (!chat.shadowRoot.querySelector('dialog[open]')) chat.shadowRoot.querySelector('.launcher')?.click();
          return {sessionId: chat.sessionId, admin: chat.hass.user?.is_admin === true,
            core: chat.hass.config?.version, isDemo: chat.demo === true};
        });
        await page.waitForFunction(() => {
          const find = window.__hababQaFind;
          const chat = find('habab-setup-chat');
          return chat?.sessionId && chat.hass?.user?.is_admin === true && !chat.busy &&
            chat.shadowRoot.querySelector('dialog[open] textarea')?.disabled === false;
        }, {timeout: STAGE_TIMEOUT.idle});
        if (!before.admin || before.core !== config.expectedCore || before.isDemo) throw fail('wrong_chat_context');
        sessionId = await page.evaluate(() => {
          const find = window.__hababQaFind;
          return find('habab-setup-chat')?.sessionId;
        });
        if (typeof sessionId !== 'string' || !sessionId) throw fail('assistant_session_missing');

        const preflight = await page.evaluate(async ({name}) => {
          const find = window.__hababQaFind;
          const chat = find('habab-setup-chat');
          const entries = await chat.hass.callWS({type: 'config_entries/get'});
          const status = await chat.hass.callWS({type: 'habab_home/setup/status', language: 'ru'});
          const all = Array.isArray(entries) ? entries : [];
          const matching = all.filter((entry) => entry.domain === 'local_todo' && entry.title === name);
          const active = (Array.isArray(status?.interactions) ? status.interactions : [])
            .filter((item) => item.domain === 'local_todo' && !['done', 'abort', 'error', 'expired'].includes(item.phase));
          return {entryIds: all.map((entry) => entry.entry_id), matchingCount: matching.length,
            localTodoCount: all.filter((entry) => entry.domain === 'local_todo').length,
            activeLocalTodoCount: active.length, statusError: Boolean(status?.error)};
        }, {name: fixtureName});
        baselineEntryIds = preflight.entryIds;
        preflightComplete = true;
        report.fixture.baseline_local_todo_entries = preflight.localTodoCount;
        report.fixture.baseline_active_local_todo_flows = preflight.activeLocalTodoCount;
        trace.identifiers = {session_id: sessionId, fixture_name: fixtureName,
          expected_storage_key: expectedFixtureKey, baseline_entry_ids: baselineEntryIds,
          interaction_id: null, config_entry_id: null, storage_key: null};
        trace.events.push({stage: 'pre_prompt_preflight', action: 'private_baseline_saved'});
        persistPrivateTrace();
        if (preflight.matchingCount || preflight.activeLocalTodoCount || preflight.statusError) {
          throw fail('local_todo_fixture_not_clean');
        }
        report.verdict = 'IN_PROGRESS';

        stage = 'chat_request';
        const firstPrompt = 'Добавь ровно одну интеграцию Local To-do (local_todo) и открой её штатную защищённую форму прямо в этом чате. Название я введу в форме. Не создавай список дел через сервисы и не подключай другие интеграции.';
        stage = 'chat_request_acceptance';
        trace.send_acceptance = {attempted: true, prompt_index: 1, accepted: false};
        trace.events.push({stage, action: 'trusted_send_click_attempted'});
        persistPrivateTrace();
        lastSubmittedPrompt = firstPrompt;
        const firstPointer = await sendChatText(page, firstPrompt);
        trace.send_acceptance.pointer = firstPointer;
        persistPrivateTrace();
        requireTrustedVisibleSend(firstPointer);
        const firstAcceptance = await verifyChatSendAccepted(page, sessionId, firstPrompt);
        trace.send_acceptance = {attempted: true, prompt_index: 1, accepted: true,
          pointer: firstPointer, evidence: firstAcceptance};
        trace.events.push({stage, action: 'chat_send_accepted', evidence: firstAcceptance});
        persistPrivateTrace();
        report.prompts_sent += 1;
        trace.events.push({stage, action: 'visible_chat_send_clicked'});
        stage = 'native_form_wait';
        await waitForNativeForm(page, sessionId);
        const card = await findFixtureCard(page);
        if (card.count !== 1 || card.phase !== 'form' || card.nativeForm !== 'step-flow-form' ||
            card.nativeContext !== 'habab-native-form-context' || !card.formReady || !card.submitEnabled ||
            card.kind !== 'config' || !card.fieldRequired || !card.fieldNames.includes('todo_list_name')) {
          throw fail('native_protected_form_not_ready');
        }
        flowInteractionId = card.interactionId;
        trace.identifiers.interaction_id = flowInteractionId;
        persistPrivateTrace();
        report.flow = {started_from_chat: true, matching_interaction_count: card.count,
          phase: card.phase, kind: card.kind, step: card.step, native_form: card.nativeForm,
          native_context: card.nativeContext, field_names: card.fieldNames,
          required_field_present: card.fieldRequired, submitted_via_visible_ui: false};
        trace.events.push({stage: 'native_form_ready', native_form: card.nativeForm,
          native_context: card.nativeContext, phase: card.phase});

        stage = 'native_form_input';
        const inputHandle = await nativeFieldHandle(page, flowInteractionId, 'todo_list_name');
        try {
          const input = inputHandle.asElement();
          if (!input) throw fail('native_todo_name_field_missing');
          await input.click();
          await input.type(fixtureName, {delay: 10});
          const valid = await input.evaluate((element, expected) => element.value === expected &&
            element.getBoundingClientRect().width > 0 && element.getBoundingClientRect().height > 0 &&
            !element.disabled, fixtureName);
          if (!valid) throw fail('native_todo_name_not_entered');
        } finally {
          await inputHandle.dispose();
        }
        const submitHandle = await page.evaluateHandle((id) => {
          const find = window.__hababQaFind;
          const chat = find('habab-setup-chat');
          return [...(chat?.integrationCard?.cards?.values?.() || [])]
            .find((item) => item.id === id && item.record?.domain === 'local_todo')?.submitButton || null;
        }, flowInteractionId);
        try {
          const submit = submitHandle.asElement();
          if (!submit) throw fail('native_submit_button_missing');
          await submit.click();
          report.flow.submitted_via_visible_ui = true;
        } finally {
          await submitHandle.dispose();
        }
        stage = 'native_submit_completion';
        await page.waitForFunction((id, expectedSession) => {
          const find = window.__hababQaFind;
          const chat = find('habab-setup-chat');
          const card = [...(chat?.integrationCard?.cards?.values?.() || [])]
            .find((item) => item.id === id && item.record?.domain === 'local_todo');
          return chat?.sessionId === expectedSession && !chat.busy && card?.record?.phase === 'done' &&
            card.record.entry_state === 'loaded';
        }, {timeout: STAGE_TIMEOUT.submit, polling: 500}, flowInteractionId, sessionId);
        const interaction = await page.evaluate((id) => {
          const find = window.__hababQaFind;
          const chat = find('habab-setup-chat');
          const item = (chat.integrationCard.interactions || []).find((value) => value.id === id);
          return item ? {phase: item.phase, entry_state: item.entry_state, entity_count: item.entity_count} : null;
        }, flowInteractionId);
        if (!interaction || interaction.phase !== 'done' || interaction.entry_state !== 'loaded') {
          throw fail('native_flow_not_loaded');
        }
        report.flow.completed_phase = interaction.phase;
        report.flow.entry_state = interaction.entry_state;
        report.flow.entity_count = interaction.entity_count;

        stage = 'independent_readback';
        let snapshot = await readHaSnapshot(page, fixtureName, baselineEntryIds);
        if (snapshot.newEntryCount === 1 && snapshot.entryDomain === 'local_todo' &&
            snapshot.entryTitleMatches) {
          createdEntryId = snapshot.entryId;
          configEntryCreated = true;
          trace.identifiers.config_entry_id = createdEntryId;
          ownedEntityIds = snapshot.ownedEntityIds;
          trace.identifiers.owned_entity_ids = ownedEntityIds;
          persistPrivateTrace();
          fixtureKey = await waitForFixtureStorageKey(config, createdEntryId, fixtureName);
          trace.identifiers.storage_key = fixtureKey;
          persistPrivateTrace();
        }
        if (!configEntryCreated || snapshot.entryState !== 'loaded' || snapshot.ownedEntityCount !== 1 ||
            snapshot.todoEntityCount !== 1 || snapshot.todoState !== '0' ||
            fixtureKey !== expectedFixtureKey) {
          throw fail('independent_local_todo_readback_failed');
        }
        const allEntries = await page.evaluate(async () => {
          const find = window.__hababQaFind;
          const chat = find('habab-setup-chat');
          return chat.hass.callWS({type: 'config_entries/get'});
        });
        const newEntryIds = (Array.isArray(allEntries) ? allEntries : [])
          .filter((entry) => !baselineEntryIds.includes(entry.entry_id));
        if (newEntryIds.length !== 1 || newEntryIds[0].entry_id !== createdEntryId ||
            newEntryIds[0].domain !== 'local_todo') throw fail('unexpected_config_entry_delta');
        report.readback = {loaded_config_entry: true, newly_created_entry_count: 1,
          owned_entity_count: snapshot.ownedEntityCount, todo_entity_count: snapshot.todoEntityCount,
          empty_todo_state: snapshot.todoState};

        stage = 'assistant_return';
        const secondPrompt = `Проверь статус именно интеграции Local To-do, которую я только что подключил. Ничего не создавай, не меняй и не удаляй. Сообщи количество её сущностей и состояние пустого списка «${fixtureName}».`;
        stage = 'assistant_return_acceptance';
        trace.send_acceptance = {attempted: true, prompt_index: 2, accepted: false};
        trace.events.push({stage, action: 'trusted_send_click_attempted'});
        persistPrivateTrace();
        lastSubmittedPrompt = secondPrompt;
        const secondPointer = await sendChatText(page, secondPrompt);
        trace.send_acceptance.pointer = secondPointer;
        persistPrivateTrace();
        requireTrustedVisibleSend(secondPointer);
        const secondAcceptance = await verifyChatSendAccepted(page, sessionId, secondPrompt);
        trace.send_acceptance = {attempted: true, prompt_index: 2, accepted: true,
          pointer: secondPointer, evidence: secondAcceptance};
        trace.events.push({stage, action: 'chat_send_accepted', evidence: secondAcceptance});
        persistPrivateTrace();
        report.prompts_sent += 1;
        stage = 'assistant_return';
        await waitForLastAssistant(page, secondPrompt, STAGE_TIMEOUT.secondTurn);
        const assistantState = await readChatRunState(page, sessionId, secondPrompt);
        if (!assistantState.sameSession || !assistantState.idle || !assistantState.assistantAfterPrompt ||
            !assistantState.assistantResponsePresent || !assistantState.chatOpen) throw fail('assistant_return_verification_failed');
        report.assistant_return = {same_session: assistantState.sameSession, idle: assistantState.idle,
          assistant_response_present: assistantState.assistantResponsePresent, chat_open: assistantState.chatOpen};
        snapshot = await readHaSnapshot(page, fixtureName, baselineEntryIds);
        if (snapshot.entryState !== 'loaded' || snapshot.todoEntityCount !== 1 || snapshot.todoState !== '0') {
          throw fail('post_assistant_readback_changed');
        }
        report.post_assistant_readback = {loaded: true, todo_entity_count: 1, empty_todo_state: '0'};
        report.verdict = 'PASS_PENDING_CLEANUP';
        trace.events.push({stage, action: 'same_session_assistant_turn_completed'});

        // In the same owned browser, clean only the exact new Local To-do entry.
        stage = 'cleanup';
        report.cleanup.attempted = true;
        const cleanupResult = await removeExactEntry(page, config, createdEntryId, fixtureName, fixtureKey,
          expectedFixtureKey, baselineEntryIds, sessionId, lastSubmittedPrompt);
        if (!cleanupResult.deleted) throw fail('exact_fixture_cleanup_rejected');
        await waitForExactCleanup(page, createdEntryId, fixtureName, baselineEntryIds, ownedEntityIds);
        const cleanupReadback = await verifyCleanup(page, createdEntryId, fixtureName, baselineEntryIds, ownedEntityIds);
        if (cleanupReadback.exactFreshEntriesRemain !== 0 || cleanupReadback.ownedEntityRegistryEntriesRemain !== 0 ||
            cleanupReadback.ownedEntityStatesRemain !== 0 || !dockerStoreAbsent(config, fixtureKey)) {
          throw fail('fixture_cleanup_incomplete');
        }
        const interactionClosed = await closeOwnInteractionCard(page, flowInteractionId, sessionId, lastSubmittedPrompt);
        if (!interactionClosed) throw fail('own_interaction_card_cleanup_failed');
        report.cleanup = {attempted: true, exact_new_entry_deleted: true,
          config_entry_absent: true, entity_registry_absent: true, entity_state_absent: true,
          local_todo_storage_file_absent: true, own_interaction_card_closed: true, broader_entries_touched: 0,
          complete: true};
        report.verdict = 'PASS';
        trace.events.push({stage, action: 'exact_fixture_removed_and_verified'});
      } catch (error) {
        callbackError = error;
      } finally {
        if (callbackError) await handleFailure(callbackError);
      }
    });
  } catch (error) {
    await handleFailure(error);
  } finally {
    report.finished_at = new Date().toISOString();
    try {
      if (config?.privateTracePath) {
        persistPrivateTrace();
      }
      if (config?.reportPath) {
        fs.writeFileSync(config.reportPath, `${JSON.stringify(report, null, 2)}\n`, {
          encoding: 'utf8', mode: 0o600, flag: 'wx',
        });
        fs.chmodSync(config.reportPath, 0o600);
      }
    } catch {
      report.verdict = report.verdict === 'PASS' ? 'PASS_REPORT_WRITE_FAILED' : report.verdict;
    }
    if (report.verdict !== 'PASS') process.exitCode = 1;
    process.stdout.write(`${JSON.stringify({verdict: report.verdict, stage, prompts_sent: report.prompts_sent,
      cleanup: report.cleanup, failure: report.failure || null, report: config?.reportPath || 'not_written'})}\n`);
  }
}

main().catch((error) => {
  process.stderr.write(`${JSON.stringify({verdict: 'BLOCKED', code: safeFailureCode(error, 'runner'),
    source_error_name: safeErrorName(error)})}\n`);
  process.exitCode = 1;
});
