"""Telegram bridge to the private Home Assistant SetupAgent."""
import asyncio
import hashlib
import json
import os
import secrets
import time
import uuid

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.storage import Store

from .const import DOMAIN, MAX_LINE, PAIR_TTL
from .websocket import async_register_commands
from .transport import worker_proxy_env


class TelegramManager:
    def __init__(self, hass):
        self.hass = hass
        self.entries = {}
        self.pairing = {}
        self.pair_locks = {}
        self.dedupe = set()
        self.dedupe_order = []
        self.store = Store(hass, 1, "habab_telegram.processed", private=True)
        self.bindings_store = Store(hass, 1, "habab_telegram.bindings", private=True)
        self.bindings = {}
        self.queues = {}
        self.tasks = {}
        self.consumer_tasks = {}
        self.turn_tasks = {}
        self.control_tasks = {}
        self.pending_sends = {}

    async def setup(self):
        self.dedupe_order = await self.store.async_load() or []
        self.dedupe_order = self.dedupe_order[-2000:]
        self.dedupe = set(self.dedupe_order)
        self.bindings = await self.bindings_store.async_load() or {}
        async_register_commands(self.hass)
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.close)

    def entry_for_owner(self, owner):
        return next((entry for entry in self.hass.config_entries.async_entries(DOMAIN)
                     if entry.data.get("owner_id") == owner), None)

    async def status(self, owner):
        entry = self.entry_for_owner(owner)
        if not entry:
            return {"phase": "not_configured"}
        state = self.entries.get(entry.entry_id)
        pair = self.pairing.get(entry.entry_id)
        binding = self.bindings.get(entry.entry_id)
        connected = bool(binding and state and state.get("ready")
                         and state.get("chat_id") == binding.get("chat_id")
                         and state.get("user_id") == binding.get("user_id"))
        if connected:
            phase = "connected"
        elif binding:
            phase = "error"
            if state is not None and not state.get("error"):
                state["error"] = "worker_not_ready"
        else:
            if not pair or time.monotonic() > pair["expires"]:
                await self.pair(owner)
                pair = self.pairing[entry.entry_id]
            phase = "pairing"
        result = {"phase": phase}
        if state and state.get("username"):
            result["bot_username"] = state["username"]
        if pair and phase == "pairing":
            result["pairing_url"] = pair["url"]
        if state and state.get("error"):
            result["error"] = state["error"]
        return result

    async def configure(self, owner, token, proxy_url=""):
        if not token.strip() or len(token) > 256:
            raise ValueError("invalid_token")
        from homeassistant.config_entries import SOURCE_USER
        result = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER, "habab_owner_id": owner},
            data={"token": token.strip(), "proxy_url": proxy_url},
        )
        if result.get("type") == "form":
            code = result.get("errors", {}).get("base", "invalid_token")
            self.hass.config_entries.flow.async_abort(result["flow_id"])
            raise ValueError(code)
        if result.get("type") == "abort" and result.get("reason") != "reconfigured":
            raise ValueError("setup_failed")
        entry = self.entry_for_owner(owner)
        if entry and entry.entry_id not in self.bindings:
            await self.pair(owner)

    async def pair(self, owner):
        entry = self.entry_for_owner(owner)
        if not entry:
            raise ValueError("not_configured")
        if not await self._owner_is_active_admin(owner):
            raise ValueError("not_allowed")
        lock = self.pair_locks.setdefault(entry.entry_id, asyncio.Lock())
        async with lock:
            self.pairing.pop(entry.entry_id, None)
            await self._clear_binding(entry)
            nonce = secrets.token_urlsafe(24)
            state = self.entries.get(entry.entry_id, {})
            username = state.get("username", entry.data["bot_username"])
            self.pairing[entry.entry_id] = {
                "hash": hashlib.sha256(nonce.encode()).hexdigest(),
                "expires": time.monotonic() + PAIR_TTL,
                "url": f"https://t.me/{username}?start={nonce}",
            }
            return {"pairing_url": self.pairing[entry.entry_id]["url"]}

    async def _owner_is_active_admin(self, owner):
        user = await self.hass.auth.async_get_user(owner)
        return bool(user and user.is_active and user.is_admin)

    async def notify_setup_complete(self, origin, domain):
        """Send a completion only to the exact currently bound private origin."""
        if not isinstance(origin, dict) or origin.get("channel") != "telegram":
            return False
        owner, entry_id, chat_id = (origin.get("owner"), origin.get("entry_id"),
                                    origin.get("chat_id"))
        origin_bot_id = origin.get("bot_id")
        if not all(isinstance(value, str) and value for value in (owner, entry_id, chat_id)):
            return False
        if type(origin_bot_id) is not int or origin_bot_id <= 0:
            return False
        if not await self._owner_is_active_admin(owner):
            return False
        entry = self.entry_for_owner(owner)
        if entry is None or entry.entry_id != entry_id:
            return False
        binding = self.bindings.get(entry_id)
        state = self.entries.get(entry_id)
        expected_bot = entry.data.get("bot_username")
        if (not binding or str(binding.get("chat_id")) != chat_id or not state
                or not state.get("ready") or state.get("chat_id") != chat_id
                or state.get("user_id") != binding.get("user_id")
                or entry.data.get("bot_id") != origin_bot_id
                or binding.get("bot_id") != origin_bot_id
                or binding.get("bot_id") != entry.data.get("bot_id")
                or not isinstance(expected_bot, str) or not expected_bot
                or str(state.get("username", "")).casefold() != expected_bot.casefold()):
            return False
        if not isinstance(domain, str) or not domain:
            return False
        sent = await self.send(entry_id, chat_id,
                               f"Настройка {domain} завершена: интеграция загружена в Home Assistant.")
        if not sent:
            return False
        # Recheck after the acknowledged send. A stale binding is never treated as
        # a successful completion delivery by the originating flow.
        if (not await self._owner_is_active_admin(owner)
                or self.entry_for_owner(owner) is None
                or self.entry_for_owner(owner).entry_id != entry_id
                or self.bindings.get(entry_id) != binding):
            return False
        return True

    async def _retry_setup_notifications(self):
        data = self.hass.data.get("habab_home", {})
        integration_setup = data.get("integration_setup")
        xiaomi_setup = data.get("xiaomi_setup")
        if integration_setup is not None:
            integration_setup.retry_pending_completions()
        if xiaomi_setup is not None:
            await xiaomi_setup.retry_pending_completions()

    async def _clear_binding(self, entry):
        """Revoke the current chat, stop its turn, and discard its session mapping."""
        state = self.entries.get(entry.entry_id)
        binding = self.bindings.pop(entry.entry_id, None)
        if state:
            chat_id = state.get("chat_id")
            state.update(chat_id=None, user_id=None)
        else:
            chat_id = (binding or {}).get("chat_id")
        if binding:
            await self.bindings_store.async_save(self.bindings)
        if chat_id is None:
            return

        turn = self.turn_tasks.get(entry.entry_id)
        if turn and not turn.done():
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
        data = self.hass.data.get("habab_home", {})
        assistant = data.get("assistant")
        if assistant is not None:
            await assistant.telegram_forget(entry.data["owner_id"], entry.entry_id, chat_id)

    async def async_setup_entry(self, entry):
        env = {k: os.environ[k] for k in ("PATH", "LANG", "TZ", "SSL_CERT_FILE", "HABAB_TELEGRAM_RELAY") if k in os.environ}
        env.update({"HOME": "/config/.habab-telegram", "NODE_NO_WARNINGS": "1"})
        proxy_env = worker_proxy_env(entry.data.get("proxy_url"))
        if proxy_env:
            env.pop("HABAB_TELEGRAM_RELAY", None)
            env.update(proxy_env)
        proc = await asyncio.create_subprocess_exec(
            "node", "/opt/habab-telegram/worker.mjs", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=env, start_new_session=True,
        )
        proc.stdin.write((json.dumps({"type": "configure", "token": entry.data["token"]}) + "\n").encode())
        await proc.stdin.drain()
        binding = self.bindings.get(entry.entry_id, {})
        if binding.get("bot_id") != entry.data.get("bot_id"):
            binding = {}
            self.bindings.pop(entry.entry_id, None)
            await self.bindings_store.async_save(self.bindings)
        state = {"process": proc, "chat_id": binding.get("chat_id"),
                 "user_id": binding.get("user_id"), "username": entry.data["bot_username"]}
        self.entries[entry.entry_id] = state
        self.queues[entry.entry_id] = asyncio.Queue(maxsize=5)
        self.tasks[entry.entry_id] = self.hass.async_create_task(self._reader(entry, state))
        self.consumer_tasks[entry.entry_id] = self.hass.async_create_task(self._consume(entry))

    async def _write(self, entry_id, payload):
        state = self.entries.get(entry_id)
        if not state:
            return False
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        if len(raw) > MAX_LINE:
            return False
        state["process"].stdin.write(raw)
        await state["process"].stdin.drain()
        return True

    @staticmethod
    def _split_send_text(chat_id, text):
        """Split only at code point boundaries so each JSON line stays bounded."""
        request_id = "0" * 32
        header = json.dumps({"type": "send", "chat_id": int(chat_id),
                             "text": "", "request_id": request_id}, ensure_ascii=False)
        available = MAX_LINE - len((header + "\n").encode())
        chunks = []
        current = []
        used = 0
        for character in text:
            encoded_size = len(json.dumps(character, ensure_ascii=False)[1:-1].encode())
            if encoded_size > available:
                raise ValueError("message_too_large")
            if used + encoded_size > available and current:
                chunks.append("".join(current))
                current = []
                used = 0
            current.append(character)
            used += encoded_size
        if current or not chunks:
            chunks.append("".join(current))
        return chunks

    async def send(self, entry_id, chat_id, text):
        try:
            chunks = self._split_send_text(chat_id, str(text))
        except (UnicodeEncodeError, ValueError):
            return False
        futures = []
        request_ids = []
        try:
            for chunk in chunks:
                request_id = uuid.uuid4().hex
                future = asyncio.get_running_loop().create_future()
                self.pending_sends[request_id] = (entry_id, future)
                if not await self._write(entry_id, {"type": "send", "chat_id": int(chat_id),
                                                    "text": chunk, "request_id": request_id}):
                    self.pending_sends.pop(request_id, None)
                    break
                request_ids.append(request_id)
                futures.append(future)
            if len(futures) != len(chunks):
                if futures:
                    await asyncio.wait_for(asyncio.gather(*futures), timeout=18)
                return False
            results = await asyncio.wait_for(asyncio.gather(*futures), timeout=18)
            return all(results)
        except TimeoutError:
            return False
        except (ValueError, BrokenPipeError, ConnectionError):
            if futures:
                try:
                    await asyncio.wait_for(asyncio.gather(*futures), timeout=18)
                except (TimeoutError, ValueError, BrokenPipeError, ConnectionError):
                    pass
            return False
        finally:
            for request_id in request_ids:
                self.pending_sends.pop(request_id, None)

    async def typing(self, entry_id, chat_id):
        try:
            await self._write(entry_id, {"type": "typing", "chat_id": int(chat_id)})
        except (ValueError, BrokenPipeError, ConnectionError):
            return False
        return True

    async def _reader(self, entry, state):
        try:
            while line := await state["process"].stdout.readline():
                if len(line) > MAX_LINE:
                    state["error"] = "worker_protocol_error"
                    return
                try:
                    payload = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    state["error"] = "worker_protocol_error"
                    return
                kind = payload.get("type")
                if kind == "ready":
                    state["username"] = payload.get("username", state["username"])
                    state["ready"] = True
                    state.pop("error", None)
                    self.hass.async_create_task(self._retry_setup_notifications())
                elif kind == "error":
                    request_id = payload.get("request_id")
                    pending = self.pending_sends.pop(request_id, None) if isinstance(request_id, str) else None
                    if pending and not pending[1].done():
                        pending[1].set_result(False)
                    if payload.get("fatal") or payload.get("operation") == "poll":
                        state["error"] = "worker_error"
                elif kind == "sent":
                    request_id = payload.get("request_id")
                    pending = self.pending_sends.pop(request_id, None) if isinstance(request_id, str) else None
                    if pending and not pending[1].done():
                        pending[1].set_result(True)
                elif kind == "message":
                    state.pop("error", None)
                    queue = self.queues[entry.entry_id]
                    route_control = self._should_route_control(entry, state, payload)
                    if not await self._claim_message(entry, payload):
                        continue
                    payload["_habab_claimed"] = True
                    if route_control:
                        task = self.hass.async_create_task(self._handle_control(entry, payload))
                        tasks = self.control_tasks.setdefault(entry.entry_id, set())
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                    elif queue.full():
                        if not await self._owner_is_active_admin(entry.data["owner_id"]):
                            if state.get("chat_id") is not None:
                                await self._clear_binding(entry)
                            else:
                                self.pairing.pop(entry.entry_id, None)
                            continue
                        await self.send(
                            entry.entry_id, payload["chat_id"],
                            "Очередь занята. Попробуйте позже.",
                        )
                    else:
                        queue.put_nowait(payload)
                        # Let the FIFO consumer begin this item before polling
                        # another update that may be a direct /cancel.
                        await asyncio.sleep(0)
            try:
                await asyncio.wait_for(state["process"].wait(), timeout=1)
            except TimeoutError:
                try:
                    os.killpg(state["process"].pid, 9)
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(state["process"].wait(), timeout=1)
            state["error"] = "worker_stopped"
        except asyncio.CancelledError:
            raise
        except Exception:
            state["error"] = "worker_error"

    def _should_route_control(self, entry, state, msg):
        value = msg.get("text")
        if not isinstance(value, str):
            return False
        text = value.strip()
        if text == "/new":
            current = self.turn_tasks.get(entry.entry_id)
            if not current or current.done():
                return False
        return (
            text in ("/cancel", "/new")
            and msg.get("chat_type") == "private"
            and all(isinstance(msg.get(field), int) for field in ("chat_id", "user_id", "message_id"))
            and str(msg["chat_id"]) == state.get("chat_id")
            and str(msg["user_id"]) == state.get("user_id")
        )

    async def _handle_control(self, entry, msg):
        await self.handle_one(entry, msg)

    async def _claim_message(self, entry, msg):
        """Persist a trusted private update before queueing any command work."""
        if msg.get("chat_type") != "private" or any(
            not isinstance(msg.get(field), int) for field in ("chat_id", "user_id", "message_id")
        ):
            return False
        if not isinstance(msg.get("text"), str):
            return False
        state = self.entries.get(entry.entry_id)
        if not state:
            return False
        chat_id, user_id = str(msg["chat_id"]), str(msg["user_id"])
        if state.get("chat_id") is None:
            pairing = self.pairing.get(entry.entry_id)
            text = msg.get("text", "")
            supplied = text[7:].strip() if text.startswith("/start ") else ""
            if not pairing or time.monotonic() > pairing["expires"] or not secrets.compare_digest(
                hashlib.sha256(supplied.encode()).hexdigest(), pairing["hash"]
            ):
                return False
        elif chat_id != state["chat_id"] or user_id != state["user_id"]:
            return False
        key = f"{entry.entry_id}:{chat_id}:{msg['message_id']}"
        if key in self.dedupe:
            return False
        self.dedupe.add(key)
        self.dedupe_order.append(key)
        if len(self.dedupe_order) > 2000:
            self.dedupe_order = self.dedupe_order[-1500:]
            self.dedupe = set(self.dedupe_order)
        await self.store.async_save(self.dedupe_order)
        return True

    async def handle_one(self, entry, msg):
        state = self.entries.get(entry.entry_id)
        if not state or msg.get("chat_type") != "private":
            return
        if any(not isinstance(msg.get(field), int) for field in ("chat_id", "user_id", "message_id")):
            return
        chat_id, user_id = str(msg.get("chat_id")), str(msg.get("user_id"))
        if state.get("chat_id") is None:
            pairing = self.pairing.get(entry.entry_id)
            text = msg.get("text", "")
            supplied = text[7:].strip() if text.startswith("/start ") else ""
            if not pairing or time.monotonic() > pairing["expires"] or not secrets.compare_digest(
                hashlib.sha256(supplied.encode()).hexdigest(), pairing["hash"]
            ):
                return
            if not await self._owner_is_active_admin(entry.data["owner_id"]):
                self.pairing.pop(entry.entry_id, None)
                return
            if self.pairing.get(entry.entry_id) is not pairing or time.monotonic() > pairing["expires"]:
                return
            if not msg.get("_habab_claimed") and not await self._claim_message(entry, msg):
                return
            state.update(chat_id=chat_id, user_id=user_id)
            self.bindings[entry.entry_id] = {"chat_id": chat_id, "user_id": user_id,
                                             "bot_id": entry.data.get("bot_id")}
            await self.bindings_store.async_save(self.bindings)
            self.pairing.pop(entry.entry_id, None)
            await self.send(entry.entry_id, chat_id, "Telegram подключён. Напишите /help для команд.")
            return
        if chat_id != state["chat_id"] or user_id != state["user_id"]:
            return
        if not await self._owner_is_active_admin(entry.data["owner_id"]):
            await self._clear_binding(entry)
            await self.send(entry.entry_id, chat_id, "Доступ отозван.")
            return
        if not msg.get("_habab_claimed") and not await self._claim_message(entry, msg):
            return
        text = msg.get("text", "").strip()
        if len(text) > 4000:
            await self.send(entry.entry_id, chat_id, "Сообщение слишком длинное (максимум 4000 символов).")
            return
        if text == "/help":
            await self.send(entry.entry_id, chat_id, "/new — новый диалог\n/cancel — остановить текущую задачу")
        elif text == "/new":
            current = self.turn_tasks.get(entry.entry_id)
            if current and not current.done():
                await self.send(entry.entry_id, chat_id, "Сначала остановите задачу командой /cancel.")
                return
            assistant = self.hass.data.get("habab_home", {}).get("assistant")
            if assistant is None:
                await self.send(entry.entry_id, chat_id, "Помощник запускается. Повторите команду позже.")
                return
            await assistant.telegram_reset(
                entry.data["owner_id"], entry.entry_id, chat_id)
            await self.send(entry.entry_id, chat_id, "Начат новый диалог.")
        elif text == "/cancel":
            current = self.turn_tasks.get(entry.entry_id)
            if current and not current.done():
                current.cancel()
                await asyncio.gather(current, return_exceptions=True)
                stopped = True
            else:
                assistant = self.hass.data.get("habab_home", {}).get("assistant")
                if assistant is None:
                    await self.send(entry.entry_id, chat_id, "Помощник запускается. Повторите команду позже.")
                    return
                mapped = assistant.telegram_sessions.get(
                    f"{entry.data['owner_id']}:{entry.entry_id}:{chat_id}")
                stopped = await assistant.abort(
                    entry.data["owner_id"], "telegram", mapped, entry.entry_id, chat_id) if mapped else False
            await self.send(entry.entry_id, chat_id, "Остановлено." if stopped else "Активной задачи нет.")
        elif text:
            current = self.turn_tasks.get(entry.entry_id)
            if current and not current.done():
                await self.send(entry.entry_id, chat_id, "Задача уже выполняется. Используйте /cancel.")
            else:
                task = self.hass.async_create_task(self._run_message(entry, chat_id, text))
                self.turn_tasks[entry.entry_id] = task

    async def _run_message(self, entry, chat_id, text):
        try:
            assistant = self.hass.data.get("habab_home", {}).get("assistant")
            if assistant is None:
                await self.send(entry.entry_id, chat_id, "Помощник запускается. Повторите запрос позже.")
                return
            session = await assistant.telegram_session(entry.data["owner_id"], entry.entry_id, chat_id)
            await self.typing(entry.entry_id, chat_id)
            result = await assistant.run(entry.data["owner_id"], "telegram", session["session_id"],
                                         text, entry.entry_id, chat_id)
            await self.send(entry.entry_id, chat_id, result["messages"][-1]["text"])
        except Exception:
            await self.send(entry.entry_id, chat_id, "Не удалось выполнить запрос. Попробуйте позже.")

    async def _consume(self, entry):
        queue = self.queues[entry.entry_id]
        while True:
            msg = await queue.get()
            try:
                await self.handle_one(entry, msg)
                turn = self.turn_tasks.get(entry.entry_id)
                if turn and not turn.done():
                    await asyncio.gather(turn, return_exceptions=True)
            except Exception:
                # Provider and HA errors are intentionally reduced to a stable user message.
                state = self.entries.get(entry.entry_id)
                if state and state.get("chat_id"):
                    await self.send(entry.entry_id, state["chat_id"], "Не удалось обработать сообщение.")
            finally:
                queue.task_done()

    async def close_entry(self, entry_id):
        state = self.entries.pop(entry_id, None)
        task = self.tasks.pop(entry_id, None)
        consumer = self.consumer_tasks.pop(entry_id, None)
        turn = self.turn_tasks.pop(entry_id, None)
        controls = self.control_tasks.pop(entry_id, set())
        for current in (task, consumer, turn, *controls):
            if current:
                current.cancel()
        await asyncio.gather(
            *(item for item in (task, consumer, turn, *controls) if item),
            return_exceptions=True,
        )
        for request_id, (send_entry_id, future) in list(self.pending_sends.items()):
            if send_entry_id == entry_id:
                if not future.done():
                    future.set_result(False)
                self.pending_sends.pop(request_id, None)
        self.queues.pop(entry_id, None)
        if state:
            proc = state["process"]
            try:
                proc.stdin.write(b'{"type":"shutdown"}\n')
                await proc.stdin.drain()
                await asyncio.wait_for(proc.wait(), 3)
            except Exception:
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass
        self.pairing.pop(entry_id, None)

    async def unlink(self, owner):
        entry = self.entry_for_owner(owner)
        if entry:
            binding = self.bindings.get(entry.entry_id, {})
            state = self.entries.get(entry.entry_id, {})
            chat_id = binding.get("chat_id", state.get("chat_id"))
            await self.close_entry(entry.entry_id)
            assistant = self.hass.data.get("habab_home", {}).get("assistant")
            if assistant is not None and chat_id is not None:
                await assistant.telegram_forget(owner, entry.entry_id, str(chat_id))
            self.bindings.pop(entry.entry_id, None)
            await self.bindings_store.async_save(self.bindings)
            await self.hass.config_entries.async_remove(entry.entry_id)

    async def close(self, _event=None):
        await asyncio.gather(*(self.close_entry(entry_id) for entry_id in list(self.entries)))


async def async_setup(hass, config):
    manager = TelegramManager(hass)
    await manager.setup()
    hass.data[DOMAIN] = manager
    return True


async def async_setup_entry(hass, entry):
    await hass.data[DOMAIN].async_setup_entry(entry)
    return True


async def async_unload_entry(hass, entry):
    await hass.data[DOMAIN].close_entry(entry.entry_id)
    return True
