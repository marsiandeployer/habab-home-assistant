import test from 'node:test';
import assert from 'node:assert/strict';
import { createTelegramRelayFetch } from '../relay-fetch.mjs';
import { TelegramTransport } from '../vendor/noxon-telegram/src/index.mjs';

const TOKEN = '12345678901234567890:unit_test_token_abcdefghijklmnop';
const response = (result, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => ({ ok: status < 300, result }),
});
const botApiUrl = (method) => `https://api.telegram.org/bot${TOKEN}/${method}`;
const aborted = (signal) => new Promise((resolve, reject) => {
  if (signal.aborted) {
    reject(signal.reason);
    return;
  }
  signal.addEventListener('abort', () => reject(signal.reason), { once: true });
});

test('direct default passes the original fetch call through without deadlines', async () => {
  const options = { method: 'POST', marker: true };
  const result = response({ result: 'direct' });
  let call;
  const relay = createTelegramRelayFetch({
    fetchImpl: async (...args) => { call = args; return result; },
    operationTimeouts: { send: 1 }, requestTimeouts: { api: 1 },
  });
  const waited = await relay.withOperation('send', async () => {
    await new Promise((resolve) => setTimeout(resolve, 5));
    return relay.fetch(botApiUrl('sendMessage'), options);
  });
  assert.equal(waited, result);
  assert.deepEqual(call, [botApiUrl('sendMessage'), options]);
});

test('relay rewrites only allowlisted Telegram methods and carries the token only as bearer auth', async () => {
  const calls = [];
  const relay = createTelegramRelayFetch({
    relayBase: 'https://relay.example.test/',
    fetchImpl: async (...args) => { calls.push(args); return response({ username: 'test_bot' }); },
  });
  for (const method of ['getMe', 'getUpdates', 'sendMessage', 'sendChatAction']) {
    const result = await relay.fetch(botApiUrl(method), { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
    assert.equal((await result.json()).ok, true);
  }
  assert.equal(calls.length, 4);
  for (const [url, options] of calls) {
    assert.equal(url.startsWith('https://relay.example.test/telegram/'), true);
    assert.equal(url.includes(TOKEN), false);
    assert.equal(options.headers.get('authorization'), `Bearer ${TOKEN}`);
    assert.equal(options.redirect, 'error');
    assert.equal(options.method, 'POST');
  }
});

test('relay URL, origin, method, and HTTP verb validation reject unsafe targets', async () => {
  const fetchImpl = async () => response({});
  for (const relayBase of [
    'http://relay.example.test',
    'https://user:pass@relay.example.test',
    'https://relay.example.test/path',
    'https://relay.example.test/?key=value',
    'https://relay.example.test/#fragment',
  ]) {
    assert.throws(() => createTelegramRelayFetch({ relayBase, fetchImpl }), { message: 'invalid_telegram_relay' });
  }
  const relay = createTelegramRelayFetch({ relayBase: 'https://relay.example.test', fetchImpl });
  await assert.rejects(relay.fetch('https://attacker.example/bot123:token/getMe', { method: 'POST' }), { message: 'invalid_telegram_operation' });
  await assert.rejects(relay.fetch(botApiUrl('getWebhookInfo'), { method: 'POST' }), { message: 'invalid_telegram_operation' });
  await assert.rejects(relay.fetch(botApiUrl('sendMessage'), { method: 'GET' }), { message: 'invalid_telegram_operation' });
});

test('request deadlines abort hung response headers and JSON body reads', async () => {
  const headersRelay = createTelegramRelayFetch({
    relayBase: 'https://relay.example.test', requestTimeouts: { api: 15, poll: 50 },
    fetchImpl: (_url, options) => aborted(options.signal),
  });
  await assert.rejects(headersRelay.fetch(botApiUrl('getMe'), { method: 'POST' }));

  const bodyRelay = createTelegramRelayFetch({
    relayBase: 'https://relay.example.test', requestTimeouts: { api: 15, poll: 50 },
    fetchImpl: (_url, options) => Promise.resolve({
      ok: true, status: 200,
      json: () => aborted(options.signal),
    }),
  });
  const responseWithPendingBody = await bodyRelay.fetch(botApiUrl('getMe'), { method: 'POST' });
  await assert.rejects(responseWithPendingBody.json());
});

test('logical send deadline bounds multi-chunk SDK work without canceling long poll and queue recovers', async () => {
  const events = [];
  let blockSend = true;
  let pollSignal;
  const relay = createTelegramRelayFetch({
    relayBase: 'https://relay.example.test',
    operationTimeouts: { configure: 100, send: 35, typing: 20 },
    requestTimeouts: { api: 1_000, poll: 1_000 },
    fetchImpl: async (url, options) => {
      const method = url.split('/').at(-1);
      if (method === 'getMe') return response({ username: 'test_bot' });
      if (method === 'getUpdates') {
        pollSignal = options.signal;
        return aborted(options.signal).then(() => response([]));
      }
      if (blockSend) return aborted(options.signal);
      return response({ message_id: 1 });
    },
  });
  const transport = new TelegramTransport({ fetchImpl: relay.fetch, emit: (event) => events.push(event), pollTimeout: 25 });
  await relay.withOperation('configure', () => transport.configure(TOKEN));
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(pollSignal && !pollSignal.aborted, 'getUpdates is live after configure leaves its logical scope');

  await relay.withOperation('send', () => transport.send(7, 'x'.repeat(8_000), 'timed-send'));
  assert.ok(events.some((event) => event.type === 'error' && event.operation === 'send' && event.request_id === 'timed-send'));
  assert.equal(pollSignal.aborted, false, 'a send deadline does not cancel the poll signal inherited at configure');

  blockSend = false;
  await relay.withOperation('send', () => transport.send(7, 'next send', 'next-send'));
  assert.ok(events.some((event) => event.type === 'sent' && event.request_id === 'next-send'), 'the serialized queue accepts later sends');
  await transport.shutdown();
  assert.equal(pollSignal.aborted, true, 'global shutdown still aborts the long poll');
});
