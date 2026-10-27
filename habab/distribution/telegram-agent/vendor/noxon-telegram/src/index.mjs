export const MAX_MESSAGE_UNITS = 3800;

/** Split text without losing Unicode code points, preferring readable boundaries. */
export function splitTelegramText(text, maxLength = MAX_MESSAGE_UNITS) {
  if (typeof text !== 'string' || !Number.isInteger(maxLength) || maxLength < 1) {
    throw new TypeError('text and positive integer maxLength are required');
  }
  if (text.length === 0) return [];

  const points = Array.from(text);
  const chunks = [];
  while (points.length > 0) {
    let size = 0;
    let units = 0;
    while (size < points.length && units + points[size].length <= maxLength) {
      units += points[size].length;
      size += 1;
    }
    if (size === 0) size = 1;
    if (size < points.length) {
      const candidate = points.slice(0, size).join('');
      const newline = candidate.lastIndexOf('\n');
      const space = candidate.lastIndexOf(' ');
      const boundary = Math.max(newline, space);
      if (boundary >= Math.floor(candidate.length * 0.6)) size = Array.from(candidate.slice(0, boundary + 1)).length;
    }
    chunks.push(points.splice(0, size).join(''));
  }
  return chunks;
}

const safeError = (operation, code, fatal = false, requestId) => ({
  type: 'error', operation, code: String(code).replace(/[^a-z0-9_]/gi, '_').slice(0, 64) || 'transport_error', fatal,
  ...(typeof requestId === 'string' ? { request_id: requestId.slice(0, 128) } : {}),
});
const sleep = (ms, signal) => new Promise((resolve) => {
  if (signal?.aborted) return resolve();
  const timer = setTimeout(resolve, ms);
  signal?.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
});

export class TelegramTransport {
  constructor({ fetchImpl = globalThis.fetch, emit = () => {}, pollTimeout = 25 } = {}) {
    this.fetch = fetchImpl;
    this.emit = emit;
    this.pollTimeout = pollTimeout;
    this.token = undefined;
    this.username = undefined;
    this.offset = 0;
    this.abortController = undefined;
    this.running = false;
    this.pollPromise = undefined;
    this.outgoing = Promise.resolve();
  }

  async configure(token) {
    if (typeof token !== 'string' || token.length < 20 || token.length > 256) throw new Error('invalid_configuration');
    this.token = token;
    this.abortController = new AbortController();
    const me = await this.api('getMe', {}, { signal: this.abortController.signal });
    if (!me?.ok || typeof me.result?.username !== 'string') throw new Error('telegram_setup_failed');
    this.username = me.result.username;
    this.running = true;
    this.emit({ type: 'ready', username: this.username });
    this.pollPromise = this.pollLoop();
  }

  async api(method, body, { signal = this.abortController?.signal, attempts = 2 } = {}) {
    let lastCode = 'telegram_unavailable';
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        const response = await this.fetch(`https://api.telegram.org/bot${this.token}/${method}`, {
          method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body), signal,
        });
        const payload = await response.json();
        if (response.ok && payload?.ok) return payload;
        if (response.status === 429 && attempt + 1 < attempts) {
          const retryAfter = Number(payload?.parameters?.retry_after);
          await sleep(Number.isFinite(retryAfter) ? Math.min(Math.max(retryAfter, 1), 3) * 1000 : 1000, signal);
          lastCode = 'telegram_rate_limited';
          continue;
        }
        lastCode = response.status === 429 ? 'telegram_rate_limited' : response.status === 409 ? 'telegram_conflict' : 'telegram_api_error';
        break;
      } catch (error) {
        if (signal?.aborted) throw new Error('shutdown');
        lastCode = 'telegram_network_error';
        if (attempt + 1 < attempts) await sleep(250 * (attempt + 1), signal);
      }
    }
    throw new Error(lastCode);
  }

  async pollLoop() {
    while (this.running) {
      try {
        const result = await this.api('getUpdates', { offset: this.offset, timeout: this.pollTimeout,
          allowed_updates: ['message', 'my_chat_member', 'chat_member'] }, { attempts: 2 });
        if (!Array.isArray(result.result)) throw new Error('telegram_api_error');
        for (const update of result.result) {
          // A response body can finish after abort; do not open a new ACK wait
          // once the worker has already rejected its pending updates on shutdown.
          if (!this.running) break;
          const message = update.message;
          if (message && (typeof message.text === 'string' || message.voice) && message.chat && message.from) {
            await this.emit({ type: 'message', update_id: Number(update.update_id),
              chat_id: Number(message.chat.id), user_id: Number(message.from.id),
              message_id: Number(message.message_id), chat_type: String(message.chat.type),
              ...(typeof message.chat.title === 'string' ? { chat_title: message.chat.title.slice(0, 256) } : {}),
              text: typeof message.text === 'string' ? message.text : '',
              ...(message.voice ? { voice: {
                file_id: String(message.voice.file_id || '').slice(0, 513),
                duration: message.voice.duration,
                ...(message.voice.file_size !== undefined ? { file_size: message.voice.file_size } : {}),
                ...(message.voice.mime_type !== undefined ? { mime_type: String(message.voice.mime_type).slice(0, 128) } : {}),
              } } : {}) });
          }
          const member = update.chat_member;
          if (member?.chat && member.new_chat_member?.user?.id) {
            this.emit({ type: 'member_update', chat_id: Number(member.chat.id),
              user_id: Number(member.new_chat_member.user.id),
              status: String(member.new_chat_member.status),
              is_member: member.new_chat_member.is_member === true,
              chat_type: String(member.chat.type) });
          }
          const botMember = update.my_chat_member;
          if (botMember?.chat && botMember.new_chat_member?.user?.id) {
            this.emit({ type: 'bot_member_update', chat_id: Number(botMember.chat.id),
              status: String(botMember.new_chat_member.status),
              chat_type: String(botMember.chat.type) });
          }
          // The bridge's async emit resolves only after native inbox persistence.
          this.offset = Math.max(this.offset, Number(update.update_id) + 1);
        }
      } catch (error) {
        if (!this.running) break;
        const code = error instanceof Error && /^[a-z0-9_]{1,64}$/i.test(error.message) ? error.message : 'telegram_poll_failed';
        const fatal = code === 'telegram_conflict';
        this.emit(safeError('poll', code, fatal));
        if (fatal) {
          this.running = false;
          this.abortController?.abort();
          break;
        }
        await sleep(1000, this.abortController?.signal);
      }
    }
  }

  send(chatId, text, requestId) {
    const operation = this.outgoing.then(async () => {
      if (!this.running) throw new Error('not_configured');
      if (!Number.isSafeInteger(chatId) || typeof text !== 'string') throw new Error('invalid_command');
      for (const chunk of splitTelegramText(text)) {
        await this.api('sendMessage', { chat_id: chatId, text: chunk }, { attempts: 2 });
      }
      this.emit({ type: 'sent', ...(typeof requestId === 'string' ? { request_id: requestId.slice(0, 128) } : {}) });
    });
    this.outgoing = operation.catch((error) => {
      const code = error instanceof Error && /^[a-z0-9_]{1,64}$/i.test(error.message) ? error.message : 'telegram_send_failed';
      this.emit(safeError('send', code, false, requestId));
    });
    return this.outgoing;
  }

  typing(chatId) {
    const operation = this.outgoing.then(async () => {
      if (!this.running || !Number.isSafeInteger(chatId)) throw new Error('invalid_command');
      await this.api('sendChatAction', { chat_id: chatId, action: 'typing' }, { attempts: 1 });
    });
    this.outgoing = operation.catch((error) => this.emit(safeError('typing', error instanceof Error ? error.message : 'telegram_typing_failed')));
    return this.outgoing;
  }

  async getChatMember(chatId, userId, requestId) {
    if (!this.running || !Number.isSafeInteger(chatId) || !Number.isSafeInteger(userId)
        || typeof requestId !== 'string') throw new Error('invalid_command');
    try {
      const result = await this.api('getChatMember', { chat_id: chatId, user_id: userId }, { attempts: 1 });
      const member = result?.result;
      if (!member || typeof member.status !== 'string') throw new Error('telegram_api_error');
      this.emit({ type: 'member', request_id: requestId.slice(0, 128), status: member.status,
        is_member: member.status !== 'left' && member.status !== 'kicked'
          && (member.status !== 'restricted' || member.is_member === true) });
    } catch (error) {
      const code = error instanceof Error && /^[a-z0-9_]{1,64}$/i.test(error.message)
        ? error.message : 'telegram_api_error';
      this.emit(safeError('member', code, false, requestId));
    }
  }

  async shutdown() {
    this.running = false;
    this.abortController?.abort();
    await this.pollPromise?.catch(() => {});
    this.token = undefined;
  }
}
