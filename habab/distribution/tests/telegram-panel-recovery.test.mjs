import test from 'node:test';
import assert from 'node:assert/strict';

const registry = new Map();
globalThis.HTMLElement = class {
  attachShadow() { this.shadowRoot = { firstChild: null }; }
};
globalThis.customElements = { get: (name) => registry.get(name), define: (name, value) => registry.set(name, value) };
await import('../src/habab_home/frontend/telegram-panel.js');
const Panel = registry.get('habab-telegram-panel');

function fixture() {
  const panel = new Panel();
  const calls = [];
  const nodes = new Map();
  panel.q = (name) => {
    if (!nodes.has(name)) nodes.set(name, { focus() {}, hidden: false, textContent: '' });
    return nodes.get(name);
  };
  panel.isConnected = true;
  panel.language = 'en';
  panel.state = { phase: 'connected', inbox: { queued: 2, processing: 1,
    recovery_required: true, recovery_id: 'a'.repeat(64) } };
  panel._hass = {
    user: { id: 'alice', is_admin: true }, connection: {},
    callWS: async (message) => {
      calls.push(message);
      return message.type.endsWith('/recover_inbox') ? { recovered: true }
        : { phase: 'connected', inbox: { queued: 0, processing: 0, recovery_required: false } };
    },
  };
  panel.updateControls = () => {};
  panel.applyState = () => panel.applyRecoveryState();
  return { panel, calls };
}

test('recovery needs a separate confirmation and cancel sends nothing', async () => {
  const { panel, calls } = fixture();
  panel.askInboxRecovery();
  assert.equal(calls.length, 0);
  panel.cancelInboxRecovery();
  await panel.confirmInboxRecovery();
  assert.equal(calls.length, 0);
  panel.askInboxRecovery();
  await panel.confirmInboxRecovery();
  assert.deepEqual(calls.map((call) => call.type), ['habab_telegram/recover_inbox', 'habab_telegram/status']);
  assert.equal(calls[0].recovery_id, 'a'.repeat(64));
  assert.equal(panel.pendingRecovery, null);
  assert.equal(panel.busy, false);
});

test('owner, connection, snapshot, admin, and mount changes cannot reuse confirmation', async () => {
  for (const mutate of [
    (panel) => { panel._hass.user = { id: 'bob', is_admin: true }; },
    (panel) => { panel._hass.connection = {}; },
    (panel) => { panel.state.inbox.recovery_id = 'b'.repeat(64); },
    (panel) => { panel._hass.user.is_admin = false; },
    (panel) => { panel.isConnected = false; },
  ]) {
    const { panel, calls } = fixture();
    panel.askInboxRecovery();
    mutate(panel);
    await panel.confirmInboxRecovery();
    assert.equal(calls.length, 0);
  }
});

test('stale recovery and lost responses reconcile status without blind retry', async () => {
  for (const stale of [true, false]) {
    const { panel, calls } = fixture();
    panel._hass.callWS = async (message) => {
      calls.push(message);
      if (message.type.endsWith('/recover_inbox')) throw stale
        ? { code: 'telegram_recovery_stale' } : new Error('fixture_response_lost');
      return { phase: 'connected', inbox: { queued: stale ? 3 : 0, processing: stale ? 1 : 0,
        recovery_required: stale, recovery_id: stale ? 'b'.repeat(64) : undefined } };
    };
    panel.askInboxRecovery();
    await panel.confirmInboxRecovery();
    assert.equal(calls.filter((call) => call.type.endsWith('/recover_inbox')).length, 1);
    assert.equal(calls.at(-1).type, 'habab_telegram/status');
    assert.equal(panel.recoveryFeedback, panel.t(stale ? 'telegramRecoveryStale' : 'telegramRecoveryStatusClear'));
  }
});

test('an old owner status response cannot arm recovery for the new owner', async () => {
  const { panel } = fixture();
  let finish;
  panel._hass.callWS = () => new Promise((resolve) => { finish = resolve; });
  const refresh = panel.refresh();
  panel._hass = { user: { id: 'bob', is_admin: true }, connection: {},
    callWS: async () => ({ phase: 'connected', inbox: { recovery_required: false } }) };
  finish({ phase: 'connected', inbox: { recovery_required: true, recovery_id: 'a'.repeat(64) } });
  await refresh;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(panel.state.inbox.recovery_required, false);
  panel.askInboxRecovery();
  assert.equal(panel.pendingRecovery, null);
});
