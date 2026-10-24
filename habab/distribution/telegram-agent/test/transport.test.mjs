import test from 'node:test';
import assert from 'node:assert/strict';
import { splitTelegramText, TelegramTransport, MAX_MESSAGE_UNITS } from '../vendor/noxon-telegram/src/index.mjs';

const response = (result, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => ({ ok: status < 300, result }) });

test('splits by safe Telegram size without damaging emoji, links, or code text', () => {
  const source = `https://example.test/a_b?x=1\n\`${'🙂'.repeat(2500)}\``;
  const chunks = splitTelegramText(source);
  assert.equal(chunks.join(''), source);
  assert.ok(chunks.length > 1);
  assert.ok(chunks.every((chunk) => chunk.length <= MAX_MESSAGE_UNITS));
  assert.ok(chunks.every((chunk) => Array.from(chunk).join('') === chunk));
  assert.ok(chunks.every((chunk) => !/[\uD800-\uDBFF]$/.test(chunk) && !/^[\uDC00-\uDFFF]/.test(chunk)));
});

test('preserves command text in updates and sends bounded chunks through Bot API', async () => {
  const events = [];
  const calls = [];
  let pollCount = 0;
  const fetchImpl = async (url, init) => {
    const method = url.split('/').at(-1);
    calls.push({ method, body: JSON.parse(init.body), url });
    if (method === 'getMe') return response({ username: 'habab_test_bot' });
    if (method === 'getUpdates') {
      pollCount += 1;
      if (pollCount === 1) return response([{ update_id: 41, message: { message_id: 8, text: '/start nonce', chat: { id: 7, type: 'private' }, from: { id: 9 } } }]);
      return new Promise((resolve) => init.signal.addEventListener('abort', () => resolve(response([])), { once: true }));
    }
    return response({ message_id: 1 });
  };
  const transport = new TelegramTransport({ fetchImpl, emit: (event) => events.push(event), pollTimeout: 1 });
  await transport.configure('12345678901234567890:unit-test-token');
  await new Promise((resolve) => setImmediate(resolve));
  const text = 'x'.repeat(3900);
  await transport.send(7, text, 'request-1');
  await transport.shutdown();

  assert.deepEqual(events.find((event) => event.type === 'ready'), { type: 'ready', username: 'habab_test_bot' });
  assert.deepEqual(events.find((event) => event.type === 'message'), {
    type: 'message', chat_id: 7, user_id: 9, message_id: 8, chat_type: 'private', text: '/start nonce',
  });
  const sends = calls.filter((call) => call.method === 'sendMessage');
  assert.equal(sends.length, 2);
  assert.equal(sends.map((call) => call.body.text).join(''), text);
  assert.ok(sends.every((call) => call.body.text.length <= 3800));
  assert.ok(events.some((event) => event.type === 'sent' && event.request_id === 'request-1'));
  assert.deepEqual(events.find((event) => event.type === 'sent'), { type: 'sent', request_id: 'request-1' });
  assert.ok(!JSON.stringify(events).includes('unit-test-token'));
});

test('polls group membership changes and emits only bounded member metadata', async () => {
  const events = [];
  let pollCount = 0;
  let allowedUpdates;
  const fetchImpl = async (url, init) => {
    const method = url.split('/').at(-1);
    const body = JSON.parse(init.body);
    if (method === 'getMe') return response({ username: 'habab_test_bot' });
    if (method === 'getUpdates') {
      allowedUpdates = body.allowed_updates;
      pollCount += 1;
      if (pollCount === 1) return response([
        { update_id: 1, message: { message_id: 2, text: '/start nonce', chat: { id: -100, type: 'supergroup', title: 'Home' }, from: { id: 9 } } },
        { update_id: 2, chat_member: { chat: { id: -100, type: 'supergroup' }, new_chat_member: { user: { id: 9 }, status: 'left' } } },
        { update_id: 3, my_chat_member: { chat: { id: -100, type: 'supergroup' }, new_chat_member: { user: { id: 77 }, status: 'administrator' } } },
      ]);
      return new Promise((resolve) => init.signal.addEventListener('abort', () => resolve(response([])), { once: true }));
    }
    return response({});
  };
  const transport = new TelegramTransport({ fetchImpl, emit: (event) => events.push(event), pollTimeout: 1 });
  await transport.configure('12345678901234567890:unit-test-token');
  await new Promise((resolve) => setImmediate(resolve));
  await transport.shutdown();
  assert.deepEqual(allowedUpdates, ['message', 'my_chat_member', 'chat_member']);
  assert.deepEqual(events.find((event) => event.type === 'message'), {
    type: 'message', chat_id: -100, user_id: 9, message_id: 2, chat_type: 'supergroup', chat_title: 'Home', text: '/start nonce',
  });
  assert.ok(events.some((event) => event.type === 'member_update'
    && event.chat_id === -100 && event.user_id === 9 && event.status === 'left'));
  assert.ok(events.some((event) => event.type === 'bot_member_update'
    && event.chat_id === -100 && event.status === 'administrator'));
});

test('getChatMember returns bounded status and fails closed on Bot API errors', async () => {
  const events = [];
  let memberReply = { status: 'restricted', is_member: true };
  const fetchImpl = async (url, init) => {
    const method = url.split('/').at(-1);
    if (method === 'getMe') return response({ username: 'x' });
    if (method === 'getUpdates') return new Promise((resolve) => init.signal.addEventListener('abort', () => resolve(response([])), { once: true }));
    if (method === 'getChatMember') return response(memberReply, memberReply.ok === false ? 403 : 200);
    return response({});
  };
  const transport = new TelegramTransport({ fetchImpl, emit: (event) => events.push(event) });
  await transport.configure('12345678901234567890:unit-test-token');
  await transport.getChatMember(-100, 9, 'member-1');
  memberReply = { ok: false, description: 'provider details must not escape' };
  await transport.getChatMember(-100, 9, 'member-2');
  await transport.shutdown();
  assert.deepEqual(events.find((event) => event.type === 'member'), {
    type: 'member', request_id: 'member-1', status: 'restricted', is_member: true,
  });
  assert.deepEqual(events.find((event) => event.type === 'error'), {
    type: 'error', operation: 'member', code: 'telegram_api_error', fatal: false, request_id: 'member-2',
  });
  assert.ok(!JSON.stringify(events).includes('provider details'));
});

test('correlates send failure and does not acknowledge undelivered text', async () => {
  const events = [];
  let sendCalls = 0;
  const fetchImpl = async (url, init) => {
    const method = url.split('/').at(-1);
    if (method === 'getMe') return response({ username: 'x' });
    if (method === 'getUpdates') return new Promise((resolve) => init.signal.addEventListener('abort', () => resolve(response([])), { once: true }));
    sendCalls += 1;
    return response({ description: 'provider details must not escape' }, 400);
  };
  const transport = new TelegramTransport({ fetchImpl, emit: (event) => events.push(event) });
  await transport.configure('12345678901234567890:unit-test-token');
  await transport.send(1, 'delivery attempt', 'correlate-7');
  await transport.shutdown();
  assert.equal(sendCalls, 1);
  assert.deepEqual(events.find((event) => event.type === 'error'), {
    type: 'error', operation: 'send', code: 'telegram_api_error', fatal: false, request_id: 'correlate-7',
  });
  assert.equal(events.some((event) => event.type === 'sent'), false);
  assert.ok(!JSON.stringify(events).includes('provider details'));
});

test('typing failure is nonfatal and polling conflict is fatal', async () => {
  const typingEvents = [];
  const typingFetch = async (url, init) => {
    const method = url.split('/').at(-1);
    if (method === 'getMe') return response({ username: 'x' });
    if (method === 'getUpdates') return new Promise((resolve) => init.signal.addEventListener('abort', () => resolve(response([])), { once: true }));
    return response({}, 400);
  };
  const typingTransport = new TelegramTransport({ fetchImpl: typingFetch, emit: (event) => typingEvents.push(event) });
  await typingTransport.configure('12345678901234567890:unit-test-token');
  await typingTransport.typing(1);
  await typingTransport.shutdown();
  assert.deepEqual(typingEvents.find((event) => event.type === 'error'), {
    type: 'error', operation: 'typing', code: 'telegram_api_error', fatal: false,
  });

  const pollEvents = [];
  const pollFetch = async (url) => url.endsWith('/getMe') ? response({ username: 'x' }) : response({}, 409);
  const pollTransport = new TelegramTransport({ fetchImpl: pollFetch, emit: (event) => pollEvents.push(event) });
  await pollTransport.configure('12345678901234567890:unit-test-token');
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(pollEvents.find((event) => event.type === 'error'), {
    type: 'error', operation: 'poll', code: 'telegram_conflict', fatal: true,
  });
  await pollTransport.shutdown();
});

test('reports rate limiting as a bounded error and retries only once', async () => {
  const events = [];
  let sends = 0;
  const fetchImpl = async (url, init) => {
    const method = url.split('/').at(-1);
    if (method === 'getMe') return response({ username: 'x' });
    if (method === 'getUpdates') return new Promise((resolve) => init.signal.addEventListener('abort', () => resolve(response([])), { once: true }));
    sends += 1;
    return response({ parameters: { retry_after: 0 }, description: 'secret provider detail' }, 429);
  };
  const transport = new TelegramTransport({ fetchImpl, emit: (event) => events.push(event) });
  await transport.configure('12345678901234567890:unit-test-token');
  await transport.send(1, 'hi');
  await transport.shutdown();
  assert.equal(sends, 2);
  assert.ok(events.some((event) => event.type === 'error' && event.code === 'telegram_rate_limited'));
  assert.ok(!JSON.stringify(events).includes('secret provider detail'));
});
