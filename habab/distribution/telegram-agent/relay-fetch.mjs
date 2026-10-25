import { AsyncLocalStorage } from 'node:async_hooks';

const TELEGRAM_ORIGIN = 'https://api.telegram.org';
const TELEGRAM_METHODS = new Set(['getMe', 'getUpdates', 'getChatMember', 'sendMessage', 'sendChatAction']);
const BOT_TOKEN_PATTERN = /^[0-9]{1,20}:[A-Za-z0-9_-]{20,256}$/;
const DEFAULT_OPERATION_TIMEOUTS = Object.freeze({ configure: 10_000, send: 14_000, typing: 5_000, member: 8_000 });
const DEFAULT_REQUEST_TIMEOUTS = Object.freeze({ api: 5_000, poll: 35_000 });
const operationContext = new AsyncLocalStorage();

function validateTransportConfig(config) {
  if (!config || typeof config !== 'object' || Array.isArray(config)) {
    throw new TypeError('invalid_telegram_transport');
  }
  const keys = Object.keys(config).sort();
  if (config.mode === 'direct') {
    if (keys.join(',') !== 'mode') throw new TypeError('invalid_telegram_transport');
    return { mode: 'direct' };
  }
  if (config.mode !== 'habab_relay' || keys.join(',') !== 'base_url,mode,protocol,service_key'
      || config.protocol !== 'telegram-bot-api-relay-v1'
      || typeof config.service_key !== 'string'
      || !/^[\x21-\x7e]{1,2000}$/.test(config.service_key)) {
    throw new TypeError('invalid_telegram_transport');
  }
  let url;
  try {
    url = new URL(config.base_url);
  } catch {
    throw new TypeError('invalid_telegram_transport');
  }
  if (url.protocol !== 'https:' || !url.hostname || url.username || url.password
      || url.search || url.hash || !['', '/'].includes(url.pathname)) {
    throw new TypeError('invalid_telegram_transport');
  }
  return { mode: 'habab_relay', origin: url.origin, serviceKey: config.service_key };
}

/** Require the explicit private configure field. Never infer a mode or fall back. */
export function telegramTransportConfig(command) {
  if (!command || typeof command !== 'object' || Array.isArray(command)) {
    throw new TypeError('invalid_telegram_transport');
  }
  if (!Object.hasOwn(command, 'telegram_transport') || Object.hasOwn(command, 'transport')) {
    throw new TypeError('invalid_telegram_transport');
  }
  const config = command.telegram_transport;
  if (!['habab_relay', 'direct'].includes(config?.mode)) throw new TypeError('invalid_telegram_transport');
  if (config.mode === 'habab_relay' && config.service_key === command.token) {
    throw new TypeError('invalid_telegram_transport');
  }
  return config;
}

function telegramMethod(input, requestMethod, strictToken) {
  let url;
  try {
    url = new URL(input);
  } catch {
    throw new TypeError('invalid_telegram_operation');
  }
  const match = url.pathname.match(/^\/bot([^/]+)\/([A-Za-z]+)$/);
  if (url.origin !== TELEGRAM_ORIGIN || url.search || url.hash || !match
      || (strictToken && !BOT_TOKEN_PATTERN.test(match[1]))
      || requestMethod !== 'POST' || !TELEGRAM_METHODS.has(match[2])) {
    throw new TypeError('invalid_telegram_operation');
  }
  return { token: match[1], method: match[2] };
}

function validTimeouts(timeouts) {
  return Object.values(timeouts).every((value) => Number.isSafeInteger(value) && value > 0);
}

function requestScope(signals, timeoutMs) {
  const controller = new AbortController();
  const listeners = [];
  const abortFrom = (signal) => {
    if (!controller.signal.aborted) controller.abort(signal.reason);
  };
  for (const signal of signals) {
    if (!signal) continue;
    if (signal.aborted) abortFrom(signal);
    else {
      const listener = () => abortFrom(signal);
      signal.addEventListener('abort', listener, { once: true });
      listeners.push([signal, listener]);
    }
  }
  const timer = setTimeout(() => {
    if (!controller.signal.aborted) controller.abort(new Error('telegram_request_timeout'));
  }, timeoutMs);
  return {
    signal: controller.signal,
    cleanup() {
      clearTimeout(timer);
      for (const [signal, listener] of listeners) signal.removeEventListener('abort', listener);
    },
  };
}

function keepDeadlineThroughJson(response, scope) {
  return new Proxy(response, {
    get(target, property) {
      if (property === 'json') {
        return async (...args) => {
          try {
            return await target.json(...args);
          } finally {
            scope.cleanup();
          }
        };
      }
      const value = Reflect.get(target, property, target);
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
}

/**
 * Build the per-config Telegram fetch adapter and logical operation deadlines.
 * `direct` uses the Bot API URL and fetch implementation unchanged apart from
 * bounded cancellation. Relay mode never sends the bot token in the relay URL.
 */
export function createTelegramRelayFetch({
  config,
  fetchImpl = globalThis.fetch,
  operationTimeouts = {},
  requestTimeouts = {},
} = {}) {
  const transport = validateTransportConfig(config);
  if (typeof fetchImpl !== 'function') throw new TypeError('invalid_telegram_fetch');
  const operationLimits = { ...DEFAULT_OPERATION_TIMEOUTS, ...operationTimeouts };
  const requestLimits = { ...DEFAULT_REQUEST_TIMEOUTS, ...requestTimeouts };
  if (!validTimeouts(operationLimits) || !validTimeouts(requestLimits)) {
    throw new TypeError('invalid_telegram_deadline');
  }

  const fetch = async (input, options = {}) => {
    const { token, method } = telegramMethod(input, options.method, transport.mode === 'habab_relay');
    if (transport.mode === 'habab_relay' && transport.serviceKey === token) {
      throw new TypeError('invalid_telegram_transport');
    }
    const isPoll = method === 'getUpdates';
    const logicalOperation = isPoll ? undefined : operationContext.getStore();
    const timeout = isPoll ? requestLimits.poll : requestLimits.api;
    const scope = requestScope([options.signal, logicalOperation?.signal], timeout);
    try {
      let url = input;
      const init = { ...options, signal: scope.signal };
      if (transport.mode === 'habab_relay') {
        const headers = new Headers(options.headers);
        headers.delete('authorization');
        headers.delete('x-habab-telegram-bot-token');
        headers.set('authorization', `Bearer ${transport.serviceKey}`);
        headers.set('x-habab-telegram-bot-token', token);
        url = `${transport.origin}/telegram/${method}`;
        init.headers = headers;
        init.redirect = 'error';
      }
      const response = await fetchImpl(url, init);
      return keepDeadlineThroughJson(response, scope);
    } catch (error) {
      scope.cleanup();
      throw error;
    }
  };

  const withOperation = async (operation, callback) => {
    const timeout = operationLimits[operation];
    if (!Number.isSafeInteger(timeout) || timeout <= 0 || typeof callback !== 'function') {
      throw new TypeError('invalid_telegram_operation');
    }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      if (!controller.signal.aborted) controller.abort(new Error('telegram_operation_timeout'));
    }, timeout);
    try {
      return await operationContext.run({ signal: controller.signal }, callback);
    } finally {
      clearTimeout(timer);
    }
  };

  return { fetch, withOperation, mode: transport.mode };
}
