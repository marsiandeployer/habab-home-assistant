import test from 'node:test';
import assert from 'node:assert/strict';
import { createTelegramRelayFetch, telegramTransportConfig } from '../relay-fetch.mjs';
import { TelegramTransport } from '../vendor/noxon-telegram/src/index.mjs';

const BOT_TOKEN = '12345678901234567890:unit_test_token_abcdefghijklmnop';
const SERVICE_KEY = 'service-key-unit-test-opaque';
const relayConfig = (overrides = {}) => ({
  mode: 'habab_relay', base_url: 'https://relay.example.test',
  service_key: SERVICE_KEY, protocol: 'telegram-bot-api-relay-v1', ...overrides,
});
const response = (result, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => ({ ok: status < 300, result }),
});
const botApiUrl = (method) => `https://api.telegram.org/bot${BOT_TOKEN}/${method}`;
const aborted = (signal) => new Promise((resolve, reject) => {
  if (signal.aborted) {
    reject(signal.reason);
    return;
  }
  signal.addEventListener('abort', () => reject(signal.reason), { once: true });
});

test('explicit direct mode preserves Bot API URL and uses the supplied fetch implementation', async () => {
  const calls = [];
  const result = response({ result: 'direct' });
  const relay = createTelegramRelayFetch({
    config: { mode: 'direct' },
    fetchImpl: async (...args) => { calls.push(args); return result; },
    operationTimeouts: { send: 100 }, requestTimeouts: { api: 100 },
  });
  const returned = await relay.withOperation('send', () => relay.fetch(
    botApiUrl('sendMessage'), { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' },
  ));
  assert.equal(returned.json instanceof Function, true);
  assert.deepEqual(calls.map(([url]) => url), [botApiUrl('sendMessage')]);
  assert.equal(calls[0][1].body, '{}');
  assert.equal(calls[0][1].headers.authorization, undefined);
  assert.equal(calls[0][1].headers['x-habab-telegram-bot-token'], undefined);
  assert.equal(calls[0][1].redirect, undefined, "direct mode retains fetch's default redirect behavior");
});

test('relay requires strict versioned configuration and separate service and bot credentials', async () => {
  const calls = [];
  const relay = createTelegramRelayFetch({
    config: relayConfig(),
    fetchImpl: async (...args) => { calls.push(args); return response({ result: 'ok' }); },
  });
  for (const method of ['getMe', 'getUpdates', 'getChatMember', 'sendMessage', 'sendChatAction']) {
    const result = await relay.fetch(botApiUrl(method), {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}',
    });
    assert.equal((await result.json()).ok, true);
  }
  assert.equal(calls.length, 5);
  for (const [url, options] of calls) {
    assert.equal(url.startsWith('https://relay.example.test/telegram/'), true);
    assert.equal(url.includes(BOT_TOKEN), false);
    assert.equal(url.includes(SERVICE_KEY), false);
    assert.equal(options.headers.get('authorization'), `Bearer ${SERVICE_KEY}`);
    assert.equal(options.headers.get('x-habab-telegram-bot-token'), BOT_TOKEN);
    assert.equal(options.redirect, 'error');
    assert.equal(options.method, 'POST');
    assert.equal(options.body, '{}');
  }
  assert.equal(JSON.stringify(calls.map(([url]) => url)).includes(BOT_TOKEN), false);
  const sameCredential = createTelegramRelayFetch({
    config: relayConfig({ service_key: BOT_TOKEN }), fetchImpl: async () => response({}),
  });
  await assert.rejects(sameCredential.fetch(botApiUrl('getMe'), { method: 'POST' }), { message: 'invalid_telegram_transport' });
});

test('invalid or ambiguous transport config fails closed without inferring direct mode', () => {
  const fetchImpl = async () => response({});
  for (const config of [
    undefined,
    { mode: 'direct', service_key: SERVICE_KEY },
    { mode: 'habab_relay', base_url: 'http://relay.example.test', service_key: SERVICE_KEY, protocol: 'telegram-bot-api-relay-v1' },
    { mode: 'habab_relay', base_url: 'https://user:pass@relay.example.test', service_key: SERVICE_KEY, protocol: 'telegram-bot-api-relay-v1' },
    { mode: 'habab_relay', base_url: 'https://relay.example.test/path', service_key: SERVICE_KEY, protocol: 'telegram-bot-api-relay-v1' },
    { mode: 'habab_relay', base_url: 'https://relay.example.test?token=x', service_key: SERVICE_KEY, protocol: 'telegram-bot-api-relay-v1' },
    { mode: 'habab_relay', base_url: 'https://relay.example.test#fragment', service_key: SERVICE_KEY, protocol: 'telegram-bot-api-relay-v1' },
    { mode: 'habab_relay', base_url: 'https://relay.example.test', service_key: SERVICE_KEY, protocol: 'wrong-version' },
  ]) {
    assert.throws(() => createTelegramRelayFetch({ config, fetchImpl }), { message: 'invalid_telegram_transport' });
  }
  assert.throws(() => telegramTransportConfig({ type: 'configure' }), { message: 'invalid_telegram_transport' });
  assert.throws(() => telegramTransportConfig({
    type: 'configure', transport: { mode: 'direct' },
  }), { message: 'invalid_telegram_transport' });
  assert.deepEqual(telegramTransportConfig({
    type: 'configure', telegram_transport: { mode: 'direct' },
  }), { mode: 'direct' });
  assert.throws(() => telegramTransportConfig({
    type: 'configure', token: BOT_TOKEN,
    telegram_transport: relayConfig({ service_key: BOT_TOKEN }),
  }), { message: 'invalid_telegram_transport' });
  assert.throws(() => createTelegramRelayFetch({
    config: relayConfig({ service_key: 'bad\r\nheader' }), fetchImpl,
  }), { message: 'invalid_telegram_transport' });
});

test('explicit direct mode preserves direct endpoint and missing mode fails closed', async () => {
  const explicit = telegramTransportConfig({
    type: 'configure', telegram_transport: { mode: 'direct' },
  });
  const directCalls = [];
  const direct = createTelegramRelayFetch({ config: explicit, fetchImpl: async (...args) => {
    directCalls.push(args); return response({ username: 'direct_bot' });
  }});
  await direct.fetch(botApiUrl('getMe'), { method: 'POST', body: '{}' });
  assert.equal(directCalls[0][0], botApiUrl('getMe'));
  assert.throws(() => telegramTransportConfig({ type: 'configure' }), { message: 'invalid_telegram_transport' });
});

test('401 relay response does not fall back to direct Telegram or reflect credentials', async () => {
  const calls = [];
  const events = [];
  const relay = createTelegramRelayFetch({ config: relayConfig(), fetchImpl: async (...args) => {
    calls.push(args);
    return response({ ok: false, description: 'private upstream text' }, 401);
  }});
  const transport = new TelegramTransport({ fetchImpl: relay.fetch, emit: (event) => events.push(event) });
  await assert.rejects(relay.withOperation('configure', () => transport.configure(BOT_TOKEN)), { message: 'telegram_api_error' });
  await transport.shutdown();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 'https://relay.example.test/telegram/getMe');
  assert.ok(!JSON.stringify(events).includes(BOT_TOKEN));
  assert.ok(!JSON.stringify(events).includes(SERVICE_KEY));
  assert.ok(!JSON.stringify(events).includes('private upstream text'));
});

test('request deadlines abort hung response headers and JSON body reads', async () => {
  const config = relayConfig();
  const headersRelay = createTelegramRelayFetch({
    config, requestTimeouts: { api: 15, poll: 50 },
    fetchImpl: (_url, options) => aborted(options.signal),
  });
  await assert.rejects(headersRelay.fetch(botApiUrl('getMe'), { method: 'POST' }));

  const bodyRelay = createTelegramRelayFetch({
    config, requestTimeouts: { api: 15, poll: 50 },
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
    config: relayConfig(),
    operationTimeouts: { configure: 100, send: 35, typing: 20, member: 30 },
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
  await relay.withOperation('configure', () => transport.configure(BOT_TOKEN));
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
