import { createInterface } from 'node:readline';
import { TelegramTransport } from 'noxon-telegram';
import { createTelegramRelayFetch, telegramTransportConfig } from './relay-fetch.mjs';

const emit = (event) => process.stdout.write(`${JSON.stringify(event)}\n`);
let relay;
const transport = new TelegramTransport({ emit, fetchImpl: (...args) => {
  if (!relay) throw new Error('configure_required');
  return relay.fetch(...args);
} });
let configured = false;
let configuring = false;
const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
const validCode = (error, fallback) => error instanceof Error && /^[a-z0-9_]{1,64}$/i.test(error.message) ? error.message : fallback;

input.on('line', async (line) => {
  if (!line || line.length > 100_000) { emit({ type: 'error', operation: configured ? 'send' : 'configure', code: 'invalid_input', fatal: !configured }); return; }
  let command;
  try { command = JSON.parse(line); } catch { emit({ type: 'error', operation: configured ? 'send' : 'configure', code: 'invalid_input', fatal: !configured }); return; }
  try {
    if (!configured && !configuring && command?.type === 'configure') {
      configuring = true;
      relay = createTelegramRelayFetch({ config: telegramTransportConfig(command) });
      await relay.withOperation('configure', () => transport.configure(command.token));
      configured = true;
      configuring = false;
    } else if (command?.type === 'send') {
      await relay.withOperation('send', () => transport.send(command.chat_id, command.text, command.request_id));
    } else if (command?.type === 'typing') {
      await relay.withOperation('typing', () => transport.typing(command.chat_id));
    } else if (command?.type === 'get_chat_member') {
      await relay.withOperation('member', () => transport.getChatMember(
        command.chat_id, command.user_id, command.request_id));
    } else if (command?.type === 'shutdown') {
      await transport.shutdown();
      input.close();
    } else {
      emit({ type: 'error', code: configured ? 'invalid_command' : 'configure_required' });
    }
  } catch (error) {
    const operation = command?.type === 'send' || command?.type === 'typing'
      ? command.type : command?.type === 'get_chat_member' ? 'member' : 'configure';
    emit({ type: 'error', operation, code: validCode(error, 'transport_error'), fatal: operation === 'configure',
      ...(operation !== 'configure' && typeof command?.request_id === 'string'
        ? { request_id: command.request_id.slice(0, 128) } : {}) });
    if (operation === 'configure') {
      configuring = false;
      await transport.shutdown();
      input.close();
    }
  }
});

input.on('close', () => { void transport.shutdown().finally(() => process.exit(0)); });
