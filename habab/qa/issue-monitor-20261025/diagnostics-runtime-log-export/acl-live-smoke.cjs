#!/usr/bin/env node
'use strict';

// Bounded Issue #16 live ACL smoke. This creates exactly one temporary HA user
// and refuses to run without the explicit --run-live switch.
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const WebSocket = require('ws');

const ORIGIN = 'http://127.0.0.1:8395';
const AUTH_FILE = '/root/.local/share/habab-owner-test/qa-current-auth.json';
const RECEIPT_DIR = __dirname;
const TIMEOUT_MS = 8000;

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function userSetDigest(users) {
  if (!Array.isArray(users)) throw new Error('invalid_user_list');
  const ids = users.map((user) => user?.id);
  if (ids.some((id) => typeof id !== 'string' || !id)) {
    throw new Error('invalid_user_list');
  }
  return sha256(JSON.stringify([...new Set(ids)].sort()));
}

function createdUserId(response) {
  const id = response?.user?.id;
  if (typeof id !== 'string' || !id) throw new Error('fixture_id_missing');
  return id;
}

async function withCreatedFixture(adminWs, createData, lifecycle) {
  let id;
  try {
    const response = await adminWs.call('config/auth/create', createData);
    id = createdUserId(response);
    await lifecycle.afterCreate(id);
    return await lifecycle.run(id);
  } finally {
    if (!id && lifecycle.recover) {
      try {
        id = await lifecycle.recover();
        if (id) await lifecycle.recovered(id);
      } catch { /* Keep the run unverified; never guess an ID to delete. */ }
    }
    if (id) await lifecycle.cleanup(id);
  }
}

function atomicReceipt(file, value) {
  const temporary = `${file}.${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp`;
  const payload = `${JSON.stringify(value, null, 2)}\n`;
  let fd;
  let renamed = false;
  try {
    fd = fs.openSync(temporary, 'wx', 0o600);
    fs.writeFileSync(fd, payload, 'utf8');
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = undefined;
    fs.chmodSync(temporary, 0o600);
    fs.renameSync(temporary, file);
    renamed = true;
    const directory = fs.openSync(path.dirname(file), 'r');
    try {
      fs.fsyncSync(directory);
    } finally {
      fs.closeSync(directory);
    }
  } catch (error) {
    if (fd !== undefined) fs.closeSync(fd);
    if (!renamed) fs.rmSync(temporary, { force: true });
    throw error;
  }
}

function requestJson(route, { method = 'GET', token, body } = {}) {
  const headers = { accept: 'application/json' };
  if (token) headers.authorization = `Bearer ${token}`;
  if (body !== undefined) headers['content-type'] = 'application/json';
  return fetch(`${ORIGIN}${route}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    redirect: 'manual',
    signal: AbortSignal.timeout(TIMEOUT_MS),
  }).then(async (response) => ({
    status: response.status,
    json: response.headers.get('content-type')?.includes('application/json')
      ? await response.json()
      : null,
  }));
}

async function postForm(route, values) {
  const response = await fetch(`${ORIGIN}${route}`, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(values),
    redirect: 'manual',
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  return response.status;
}

function refreshOwnerToken(auth, fileStat) {
  if (typeof auth.access_token !== 'string' || !auth.access_token ||
      typeof auth.refresh_token !== 'string' || !auth.refresh_token ||
      typeof auth.client_id !== 'string' || !auth.client_id) {
    throw new Error('canonical_auth_incomplete');
  }
  const lifetime = Number(auth.expires_in);
  const expiresAt = fileStat.mtimeMs + lifetime * 1000;
  if (!Number.isFinite(lifetime) || lifetime <= 0 ||
      expiresAt - Date.now() < 10 * 60 * 1000) {
    const body = new URLSearchParams({
      grant_type: 'refresh_token',
      refresh_token: auth.refresh_token,
      client_id: auth.client_id,
    }).toString();
    return new Promise((resolve, reject) => {
      const req = http.request(`${ORIGIN}/auth/token`, {
        method: 'POST',
        headers: {
          'content-type': 'application/x-www-form-urlencoded',
          'content-length': Buffer.byteLength(body),
        },
        timeout: TIMEOUT_MS,
      }, (res) => {
        let response = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => { response += chunk; });
        res.on('end', () => {
          try {
            const result = JSON.parse(response);
            if (res.statusCode !== 200 || typeof result.access_token !== 'string') {
              reject(new Error('canonical_refresh_failed'));
              return;
            }
            resolve(result.access_token);
          } catch {
            reject(new Error('canonical_refresh_failed'));
          }
        });
      });
      req.on('timeout', () => req.destroy(new Error('canonical_refresh_timeout')));
      req.on('error', () => reject(new Error('canonical_refresh_failed')));
      req.end(body);
    });
  }
  return Promise.resolve(auth.access_token);
}

function connectWs(token) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket('ws://127.0.0.1:8395/api/websocket');
    const pending = new Map();
    let sequence = 0;
    let settled = false;
    const timer = setTimeout(() => finish(new Error('ws_timeout')), TIMEOUT_MS);
    const finish = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) {
        ws.close();
        reject(error);
      } else {
        resolve({
          call(type, data = {}) {
            return new Promise((ok, fail) => {
              const id = ++sequence;
              const callTimer = setTimeout(() => {
                pending.delete(id);
                fail(new Error('ws_call_timeout'));
              }, TIMEOUT_MS);
              pending.set(id, { id, ok, fail, timer: callTimer });
              ws.send(JSON.stringify({ id, type, ...data }));
            });
          },
          close() { ws.close(); },
        });
      }
    };
    ws.on('message', (buffer) => {
      let message;
      try { message = JSON.parse(buffer.toString()); } catch {
        finish(new Error('ws_invalid_message'));
        return;
      }
      if (message.type === 'auth_required') {
        ws.send(JSON.stringify({ type: 'auth', access_token: token }));
      } else if (message.type === 'auth_ok') {
        finish();
      } else if (message.type === 'auth_invalid') {
        finish(new Error('ws_auth_failed'));
      } else if (Number.isInteger(message.id) && pending.has(message.id)) {
        const task = pending.get(message.id);
        pending.delete(message.id);
        clearTimeout(task.timer);
        if (message.type !== 'result' || message.success !== true || message.id !== task.id) {
          task.fail(new Error('ws_command_failed'));
        } else {
          task.ok(message.result);
        }
      }
    });
    ws.on('error', () => finish(new Error('ws_connect_failed')));
    ws.on('close', () => {
      for (const task of pending.values()) {
        clearTimeout(task.timer);
        task.fail(new Error('ws_closed'));
      }
      pending.clear();
    });
  });
}

async function loginFixture(username, password, checkStop) {
  const clientId = `${ORIGIN}/`;
  const start = await requestJson('/auth/login_flow', {
    method: 'POST',
    body: {
      client_id: clientId,
      handler: ['homeassistant', null],
      redirect_uri: `${clientId}?auth_callback=1`,
    },
  });
  checkStop();
  if (start.status !== 200 || !start.json?.flow_id) throw new Error('login_flow_start_failed');
  const next = await requestJson(`/auth/login_flow/${encodeURIComponent(start.json.flow_id)}`, {
    method: 'POST',
    body: { username, password, client_id: clientId },
  });
  checkStop();
  if (next.status !== 200 || typeof next.json?.result !== 'string') {
    throw new Error('login_flow_complete_failed');
  }
  const form = new URLSearchParams({
    grant_type: 'authorization_code',
    code: next.json.result,
    client_id: clientId,
  }).toString();
  const tokenResult = await new Promise((resolve, reject) => {
    const req = http.request(`${ORIGIN}/auth/token`, {
      method: 'POST',
      headers: {
        'content-type': 'application/x-www-form-urlencoded',
        'content-length': Buffer.byteLength(form),
      },
      timeout: TIMEOUT_MS,
    }, (res) => {
      let response = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { response += chunk; });
      res.on('end', () => {
        try {
          const result = JSON.parse(response);
          if (res.statusCode !== 200 || typeof result.access_token !== 'string') {
            reject(new Error('fixture_token_failed'));
          } else resolve(result);
        } catch { reject(new Error('fixture_token_failed')); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('fixture_token_timeout')));
    req.on('error', () => reject(new Error('fixture_token_failed')));
    req.end(form);
  });
  checkStop();
  return tokenResult;
}

async function main() {
  if (!process.argv.includes('--run-live')) {
    console.error('Refusing live calls. Re-run only with --run-live after owner authorization.');
    process.exitCode = 2;
    return;
  }
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  const receiptPath = path.join(RECEIPT_DIR, `acl-live-smoke-${timestamp}.json`);
  const receipt = {
    scope: 'issue16_live_acl_smoke',
    status: 'in_progress',
    started_at: new Date().toISOString(),
    endpoint: 'owner-test-loopback-8395',
    before_user_set_sha256: null,
    after_user_set_sha256: null,
    created_user_id_sha256: null,
    assertions: {
      nonadmin_list_and_export_denied: null,
      promoted_admin_own_list_empty: null,
      foreign_detail_hidden: null,
      exact_fixture_absent_after_cleanup: null,
    },
    cleanup_state: 'not_started',
    failure_stage: null,
  };
  atomicReceipt(receiptPath, receipt);

  let stage = 'preflight';
  let adminWs;
  let fixtureWs;
  let createdId;
  let fixtureRefreshToken;
  let baselineUsers;
  let foreignSessionId;
  let failed = false;
  let fixtureToken;
  let receiptWriteFailed = false;
  let stopRequested = false;
  const onStop = () => { stopRequested = true; };
  const checkStop = () => {
    if (stopRequested) throw new Error('interrupted');
  };
  process.on('SIGINT', onStop);
  process.on('SIGTERM', onStop);
  try {
    const authStat = fs.statSync(AUTH_FILE);
    if ((authStat.mode & 0o077) !== 0) throw new Error('canonical_auth_permissions');
    const auth = JSON.parse(fs.readFileSync(AUTH_FILE, 'utf8'));
    const ownerToken = await refreshOwnerToken(auth, authStat);
    checkStop();
    adminWs = await connectWs(ownerToken);
    checkStop();
    const owner = await adminWs.call('auth/current_user');
    checkStop();
    if (owner?.is_owner !== true || owner?.is_admin !== true) throw new Error('owner_admin_required');
    baselineUsers = await adminWs.call('config/auth/list');
    checkStop();
    receipt.before_user_set_sha256 = userSetDigest(baselineUsers);
    const baselineIds = new Set(baselineUsers.map((user) => user.id));
    const sessions = await requestJson('/api/habab_home/diagnostics/sessions?channel=web&limit=1', {
      token: ownerToken,
    });
    checkStop();
    foreignSessionId = sessions.json?.sessions?.[0]?.session_id;
    if (sessions.status !== 200 || typeof foreignSessionId !== 'string') {
      throw new Error('foreign_session_unavailable');
    }
    atomicReceipt(receiptPath, receipt);

    stage = 'fixture_create';
    const suffix = crypto.randomBytes(4).toString('hex');
    const fixtureName = `Temporary ACL QA ${suffix}`;
    const username = `qa-acl-${crypto.randomBytes(16).toString('hex')}`;
    const password = crypto.randomBytes(32).toString('base64url');
    receipt.fixture_name_sha256 = sha256(fixtureName);
    receipt.cleanup_state = 'pending';
    atomicReceipt(receiptPath, receipt);
    checkStop();
    await withCreatedFixture(adminWs, {
      name: fixtureName,
      group_ids: ['system-users'],
      local_only: true,
    }, {
      recover: async () => {
        const users = await adminWs.call('config/auth/list');
        if (!Array.isArray(users)) throw new Error('invalid_user_list');
        const candidates = users.filter((user) => !baselineIds.has(user.id) &&
          user.name === fixtureName && user.local_only === true && user.is_owner !== true &&
          user.system_generated !== true && Array.isArray(user.group_ids) &&
          user.group_ids.includes('system-users'));
        return candidates.length === 1 ? candidates[0].id : undefined;
      },
      recovered: async (id) => {
        createdId = id;
        receipt.create_response_recovered_by_exact_name = true;
        receipt.created_user_id_sha256 = sha256(id);
        atomicReceipt(receiptPath, receipt);
      },
      afterCreate: async (id) => {
        createdId = id;
        receipt.created_user_id_sha256 = sha256(id);
        atomicReceipt(receiptPath, receipt);
        checkStop();
        await adminWs.call('config/auth_provider/homeassistant/create', {
          user_id: id, username, password,
        });
        checkStop();
      },
      run: async (id) => {
        stage = 'nonadmin_denial';
        fixtureToken = await loginFixture(username, password, checkStop);
        checkStop();
        fixtureRefreshToken = fixtureToken.refresh_token;
        fixtureWs = await connectWs(fixtureToken.access_token);
        const nonadmin = await fixtureWs.call('auth/current_user');
        checkStop();
        if (nonadmin?.id !== id || nonadmin?.is_admin !== false) {
          throw new Error('fixture_nonadmin_identity_mismatch');
        }
        const listDenied = await requestJson('/api/habab_home/diagnostics/sessions?channel=web&limit=1', {
          token: fixtureToken.access_token,
        });
        checkStop();
        const exportDenied = await requestJson('/api/habab_home/diagnostics/export', {
          method: 'POST', token: fixtureToken.access_token, body: {},
        });
        checkStop();
        receipt.assertions.nonadmin_list_and_export_denied =
          listDenied.status === 403 && exportDenied.status === 403;
        atomicReceipt(receiptPath, receipt);

        stage = 'promote_fixture';
        await adminWs.call('config/auth/update', {
          user_id: id, group_ids: ['system-admin'],
        });
        checkStop();
        fixtureWs.close();
        fixtureWs = await connectWs(fixtureToken.access_token);
        const promoted = await fixtureWs.call('auth/current_user');
        checkStop();
        if (promoted?.id !== id || promoted?.is_admin !== true) {
          throw new Error('fixture_admin_identity_mismatch');
        }
        const ownList = await requestJson('/api/habab_home/diagnostics/sessions?channel=web&limit=20', {
          token: fixtureToken.access_token,
        });
        checkStop();
        receipt.assertions.promoted_admin_own_list_empty =
          ownList.status === 200 && Array.isArray(ownList.json?.sessions) && ownList.json.sessions.length === 0;
        const foreignDetail = await requestJson(
          `/api/habab_home/diagnostics/sessions/${encodeURIComponent(foreignSessionId)}?channel=all`,
          { token: fixtureToken.access_token },
        );
        checkStop();
        receipt.assertions.foreign_detail_hidden = foreignDetail.status === 404;
        atomicReceipt(receiptPath, receipt);
      },
      cleanup: async (id) => {
        receipt.cleanup_state = 'pending';
        try { atomicReceipt(receiptPath, receipt); } catch { receiptWriteFailed = true; }
        if (fixtureWs) fixtureWs.close();
        if (fixtureRefreshToken) {
          try {
            receipt.fixture_refresh_token_revoked = await postForm('/auth/revoke', {
              token: fixtureRefreshToken,
              client_id: `${ORIGIN}/`,
            }) === 200;
          } catch { receipt.fixture_refresh_token_revoked = false; }
        }
        try { await adminWs.call('config/auth/delete', { user_id: id }); } catch {
          receipt.fixture_delete_command_result = 'error';
        }
        try {
          const remaining = await adminWs.call('config/auth/list');
          const remainingIds = new Set(remaining.map((user) => user.id));
          receipt.assertions.exact_fixture_absent_after_cleanup = !remainingIds.has(id);
          receipt.after_user_set_sha256 = userSetDigest(remaining);
          receipt.cleanup_state = receipt.assertions.exact_fixture_absent_after_cleanup &&
            receipt.after_user_set_sha256 === receipt.before_user_set_sha256 ? 'verified' : 'failed';
        } catch {
          receipt.cleanup_state = 'failed';
          receipt.assertions.exact_fixture_absent_after_cleanup = false;
        }
        try { atomicReceipt(receiptPath, receipt); } catch { receiptWriteFailed = true; }
      },
    });
  } catch (error) {
    failed = true;
    receipt.failure_stage = stage;
    receipt.failure_code = error.message === 'interrupted' ? 'interrupted' : 'operation_failed';
  } finally {
    if (stopRequested) {
      failed = true;
      receipt.failure_stage ||= 'interrupted';
      receipt.failure_code ||= 'interrupted';
    }
    receipt.cleanup_state = createdId ? receipt.cleanup_state : 'not_needed';
    try { atomicReceipt(receiptPath, receipt); } catch { receiptWriteFailed = true; }
    if (fixtureWs) fixtureWs.close();
    if (!createdId && baselineUsers && adminWs) {
      try {
        const remaining = await adminWs.call('config/auth/list');
        receipt.after_user_set_sha256 = userSetDigest(remaining);
        receipt.cleanup_state = receipt.after_user_set_sha256 === receipt.before_user_set_sha256
          ? 'verified_no_fixture_created' : 'failed';
        receipt.assertions.exact_fixture_absent_after_cleanup =
          receipt.after_user_set_sha256 === receipt.before_user_set_sha256;
      } catch { receipt.cleanup_state = 'failed'; }
    }
    receipt.finished_at = new Date().toISOString();
    const assertions = Object.values(receipt.assertions);
    receipt.status = !failed && !receiptWriteFailed && receipt.cleanup_state.startsWith('verified') &&
      assertions.every((value) => value === true) ? 'PASS' : 'UNVERIFIED';
    try { atomicReceipt(receiptPath, receipt); } catch { receiptWriteFailed = true; }
    if (receiptWriteFailed) receipt.status = 'UNVERIFIED';
    if (adminWs) adminWs.close();
    process.off('SIGINT', onStop);
    process.off('SIGTERM', onStop);
  }
  console.log(JSON.stringify({
    status: receipt.status,
    receipt: path.basename(receiptPath),
    cleanup_state: receipt.cleanup_state,
  }));
  if (receipt.status !== 'PASS') process.exitCode = 1;
}

if (require.main === module) main().catch(() => {
  console.error('ACL smoke failed before a sanitized receipt could be finalized.');
  process.exitCode = 1;
});

module.exports = { sha256, userSetDigest, atomicReceipt, withCreatedFixture };
