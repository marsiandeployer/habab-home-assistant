import { AsyncLocalStorage } from 'node:async_hooks';

const TELEGRAM_ORIGIN = 'https://api.telegram.org';
const TELEGRAM_METHODS = new Set(['getMe', 'getUpdates', 'sendMessage', 'sendChatAction']);
const DEFAULT_OPERATION_TIMEOUTS = Object.freeze({ configure: 10_000, send: 14_000, typing: 5_000 });
const DEFAULT_REQUEST_TIMEOUTS = Object.freeze({ api: 5_000, poll: 35_000 });
const operationContext = new AsyncLocalStorage();

function relayOrigin(value) {
  if (value === undefined || value === null || value === '') return undefined;
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new TypeError('invalid_telegram_relay');
  }
  if (url.protocol !== 'https:' || !url.hostname || url.username || url.password
      || url.search || url.hash || !['', '/'].includes(url.pathname)) {
    throw new TypeError('invalid_telegram_relay');
  }
  return url.origin;
}

function telegramMethod(input, requestMethod) {
  let url;
  try {
    url = new URL(input);
  } catch {
    throw new TypeError('invalid_telegram_operation');
  }
  const match = url.pathname.match(/^\/bot([0-9]{1,20}:[A-Za-z0-9_-]{20,256})\/([A-Za-z]+)$/);
  if (url.origin !== TELEGRAM_ORIGIN || url.search || url.hash || !match
      || requestMethod !== 'POST' || !TELEGRAM_METHODS.has(match[2])) {
    throw new TypeError('invalid_telegram_operation');
  }
  return { token: match[1], method: match[2] };
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
 * Create the Telegram SDK fetch adapter and logical operation deadline wrapper.
 * When relayBase is unset, fetch and withOperation preserve direct-mode behavior.
 */
export function createTelegramRelayFetch({
  relayBase,
  fetchImpl = globalThis.fetch,
  operationTimeouts = {},
  requestTimeouts = {},
} = {}) {
  const origin = relayOrigin(relayBase);
  if (typeof fetchImpl !== 'function') throw new TypeError('invalid_telegram_fetch');
  const operationLimits = { ...DEFAULT_OPERATION_TIMEOUTS, ...operationTimeouts };
  const requestLimits = { ...DEFAULT_REQUEST_TIMEOUTS, ...requestTimeouts };

  const fetch = async (input, options = {}) => {
    if (!origin) return fetchImpl(input, options);
    const { token, method } = telegramMethod(input, options.method);
    const isPoll = method === 'getUpdates';
    const logicalOperation = isPoll ? undefined : operationContext.getStore();
    const timeout = isPoll ? requestLimits.poll : requestLimits.api;
    const scope = requestScope([options.signal, logicalOperation?.signal], timeout);
    const headers = new Headers(options.headers);
    headers.delete('authorization');
    headers.set('authorization', `Bearer ${token}`);
    try {
      const response = await fetchImpl(`${origin}/telegram/${method}`, {
        ...options,
        headers,
        signal: scope.signal,
        redirect: 'error',
      });
      return keepDeadlineThroughJson(response, scope);
    } catch (error) {
      scope.cleanup();
      throw error;
    }
  };

  const withOperation = async (operation, callback) => {
    if (!origin) return callback();
    const timeout = operationLimits[operation];
    if (!Number.isFinite(timeout) || timeout <= 0 || typeof callback !== 'function') {
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

  return { fetch, withOperation };
}
