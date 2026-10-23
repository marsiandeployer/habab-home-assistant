import { createInterface } from 'node:readline';
import { TelegramTransport } from 'noxon-telegram';

const emit = (event) => process.stdout.write(`${JSON.stringify(event)}\n`);
let relay;
if (process.env.HABAB_TELEGRAM_RELAY) {
  const url = new URL(process.env.HABAB_TELEGRAM_RELAY);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || !['', '/'].includes(url.pathname)) throw new Error('invalid_telegram_relay');
  relay = url.origin;
}
const fetchImpl = (url, options) => {
  if (!relay) return globalThis.fetch(url, options);
  const prefix = `https://api.telegram.org/bot${transport.token}/`;
  if (!url.startsWith(prefix)) throw new Error('invalid_telegram_operation');
  const method = url.slice(prefix.length);
  if (!['getMe', 'getUpdates', 'sendMessage', 'sendChatAction'].includes(method)) throw new Error('invalid_telegram_operation');
  return globalThis.fetch(`${relay}/telegram/${method}`, {...options,
    headers: {...options.headers, authorization: `Bearer ${transport.token}`}});
};
const transport = new TelegramTransport({ emit, fetchImpl });
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
      await transport.configure(command.token);
      configured = true;
      configuring = false;
    } else if (command?.type === 'send') {
      await transport.send(command.chat_id, command.text, command.request_id);
    } else if (command?.type === 'typing') {
      await transport.typing(command.chat_id);
    } else if (command?.type === 'shutdown') {
      await transport.shutdown();
      input.close();
    } else {
      emit({ type: 'error', code: configured ? 'invalid_command' : 'configure_required' });
    }
  } catch (error) {
    const operation = command?.type === 'send' || command?.type === 'typing' ? command.type : 'configure';
    emit({ type: 'error', operation, code: validCode(error, 'transport_error'), fatal: operation === 'configure',
      ...(operation === 'send' && typeof command?.request_id === 'string' ? { request_id: command.request_id.slice(0, 128) } : {}) });
    if (operation === 'configure') {
      configuring = false;
      await transport.shutdown();
      input.close();
    }
  }
});

input.on('close', () => { void transport.shutdown().finally(() => process.exit(0)); });
