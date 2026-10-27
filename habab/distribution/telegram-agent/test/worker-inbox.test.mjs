import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { mkdtemp, writeFile, unlink, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Replace fetch before importing the actual worker. No sockets or Bot API calls.
const preload = `
globalThis.fetch = async (input, init) => {
  const url = new URL(input);
  if (url.origin !== 'https://api.telegram.org') throw new Error('unexpected_fixture_route');
  const method = url.pathname.split('/').at(-1);
  const body = JSON.parse(init.body);
  process.stderr.write(JSON.stringify({method, offset: body.offset}) + '\\n');
  let result;
  if (method === 'getMe') result = {username: 'fixture_bot'};
  else if (method === 'getUpdates' && body.offset === 0) result = [{
    update_id: 41, message: {message_id: 8, text: 'fixture',
      chat: {id: 7, type: 'private'}, from: {id: 9}}
  }];
  else if (method === 'getUpdates') return new Promise((resolve) => {
    init.signal.addEventListener('abort', () => resolve({ok: true, status: 200,
      json: async () => ({ok: true, result: []})}), {once: true});
  });
  else throw new Error('unexpected_fixture_operation');
  return {ok: true, status: 200, json: async () => ({ok: true, result})};
};`;

const waitFor = async (predicate) => {
  const deadline = Date.now() + 2000;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error('fixture_worker_timeout');
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
};

async function withFixtureWorker(env, run) {
  const events = [], calls = [];
  const child = spawn(process.execPath, [
    '--import', `data:text/javascript,${encodeURIComponent(preload)}`,
    new URL('../worker.mjs', import.meta.url).pathname,
  ], { env, stdio: ['pipe', 'pipe', 'pipe'] });
  const output = createInterface({ input: child.stdout });
  const diagnostics = createInterface({ input: child.stderr });
  let closed = false;
  child.once('close', () => { closed = true; });
  output.on('line', (line) => events.push(JSON.parse(line)));
  diagnostics.on('line', (line) => calls.push(JSON.parse(line)));
  const command = (value) => child.stdin.write(JSON.stringify(value) + '\n');
  const stop = async () => {
    command({ type: 'shutdown' });
    await waitFor(() => closed);
    assert.equal(child.exitCode, 0);
  };
  try {
    await run({ events, calls, command, stop });
  } finally {
    if (!closed) child.kill('SIGKILL');
    await waitFor(() => closed);
    output.close();
    diagnostics.close();
  }
}

const configure = { type: 'configure', token: '123456789:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  telegram_transport: { mode: 'direct' } };

test('actual worker confirms only the matching persisted update and shuts down while ACK is pending', async () => {
  for (const acknowledge of [true, false]) {
    await withFixtureWorker({}, async ({ events, calls, command, stop }) => {
      command(configure);
      await waitFor(() => events.some((event) => event.type === 'message'));
      assert.deepEqual(calls.filter((call) => call.method === 'getUpdates').map((call) => call.offset), [0]);
      command({ type: 'ack_update', update_id: 999 });
      await new Promise((resolve) => setTimeout(resolve, 20));
      assert.equal(calls.filter((call) => call.method === 'getUpdates').length, 1);
      if (acknowledge) {
        command({ type: 'ack_update', update_id: 41 });
        await waitFor(() => calls.some((call) => call.offset === 42));
      }
      await stop();
      assert.equal(calls.some((call) => call.method === 'sendMessage'), false);
      assert.equal(events.filter((event) => event.type === 'message').length, 1);
    });
  }
});

test('maintenance worker performs zero Bot API requests; removing marker resumes only a fresh startup', async () => {
  const root = await mkdtemp(join(tmpdir(), 'telegram-worker-maintenance-'));
  const marker = join(root, '.habab-telegram-maintenance');
  const env = { HABAB_TELEGRAM_MAINTENANCE_FILE: marker };
  try {
    await writeFile(marker, '');
    await withFixtureWorker(env, async ({ events, calls, command, stop }) => {
      for (const value of [configure, { type: 'send', chat_id: 7, text: 'fixture' },
        { type: 'typing', chat_id: 7 }, { type: 'get_chat_member', chat_id: 7, user_id: 9 },
        { type: 'ack_update', update_id: 41 }]) command(value);
      await waitFor(() => events.length === 5);
      assert.ok(events.every((event) => event.code === 'telegram_maintenance'));
      assert.deepEqual(calls, []); // No getMe, getUpdates, higher offset, or send.
      await unlink(marker);
      command(configure);
      await waitFor(() => events.length === 6);
      assert.equal(events.at(-1).code, 'telegram_maintenance');
      assert.deepEqual(calls, []); // Removing a marker never hot-resumes this worker.
      await stop();
    });
    await withFixtureWorker(env, async ({ events, calls, command, stop }) => {
      command(configure);
      await waitFor(() => events.some((event) => event.type === 'message'));
      assert.ok(events.some((event) => event.type === 'ready'));
      assert.deepEqual(calls.map((call) => call.method), ['getMe', 'getUpdates']);
      assert.equal(calls.at(-1).offset, 0);
      await stop();
    });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
