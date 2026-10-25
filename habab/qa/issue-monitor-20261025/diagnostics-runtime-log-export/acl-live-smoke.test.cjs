'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { atomicReceipt, userSetDigest, withCreatedFixture } = require('./acl-live-smoke.cjs');

test('user-set digest is order independent and distinguishes added IDs', () => {
  const users = [{ id: 'uuid-a' }, { id: 'uuid-b' }];
  assert.equal(userSetDigest(users), userSetDigest([...users].reverse()));
  assert.notEqual(userSetDigest(users), userSetDigest([...users, { id: 'uuid-c' }]));
  assert.equal(userSetDigest([]), crypto.createHash('sha256').update('[]').digest('hex'));
});

test('receipt rewrite is atomic, mode 0600, and contains only supplied sanitized fields', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'habab-acl-receipt-'));
  const file = path.join(directory, 'receipt.json');
  try {
    atomicReceipt(file, { status: 'in_progress', created_user_id_sha256: 'a'.repeat(64) });
    atomicReceipt(file, { status: 'UNVERIFIED', exact_fixture_absent: false });
    const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.deepEqual(saved, { status: 'UNVERIFIED', exact_fixture_absent: false });
    assert.equal(fs.statSync(file).mode & 0o777, 0o600);
    assert.deepEqual(fs.readdirSync(directory), ['receipt.json']);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test('created user response feeds the exact UUID to finally cleanup after run failure', async () => {
  const calls = [];
  const adminWs = {
    async call(type, data) {
      calls.push({ type, data });
      if (type === 'config/auth/create') return { user: { id: 'fixture-user-uuid' } };
      return undefined;
    },
  };
  await assert.rejects(withCreatedFixture(adminWs, { name: 'Temporary ACL QA test' }, {
    afterCreate: async (id) => assert.equal(id, 'fixture-user-uuid'),
    run: async () => { throw new Error('injected_run_failure'); },
    cleanup: async (id) => adminWs.call('config/auth/delete', { user_id: id }),
  }), /injected_run_failure/);
  assert.deepEqual(calls, [
    { type: 'config/auth/create', data: { name: 'Temporary ACL QA test' } },
    { type: 'config/auth/delete', data: { user_id: 'fixture-user-uuid' } },
  ]);
});
