"""Telegram bridge to the private Home Assistant SetupAgent."""
import asyncio
import hashlib
import json
import os
import secrets
import time
import uuid

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.config_entries import ConfigEntryChange, SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.storage import Store
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    BOT_ONBOARDING_MESSAGE, DOMAIN, GROUP_PAIR_TTL, MAX_LINE,
    OWNER_PAIRING_COMPLETE_MESSAGE, PAIR_TTL, UNKNOWN_MEMBER_MESSAGE,
)
from .websocket import async_register_commands
from .inbox import InboxStore, TelegramInbox
from .maintenance import maintenance_enabled, maintenance_path
from .transport import (
    TransportError, resolve_telegram_transport, telegram_transport_identity,
    worker_proxy_env,
)


PROVIDER_DOMAIN = "habab_home"


class TelegramManager:
    CONTROL_TASK_LIMIT = 16

    def __init__(self, hass):
        self.hass = hass
        self.maintenance = maintenance_enabled(hass)
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
        self.control_lanes = {}
        self.pending_message_ids = {}
        self.cancelled_through = {}
        self.cancel_epochs = {}
        self.pending_group_cancels = {}
        self.pending_sends = {}
        self.inbox = TelegramInbox(InboxStore(hass))
        self.inbox_dispatched = set()
        self.waiting_updates = {}
        self.pending_members = {}
        self.group_pairing = {}
        self.group_candidates = {}
        self.group_candidate_tasks = {}
        self.group_bindings_store = Store(hass, 1, "habab_telegram.group_bindings", private=True)
        self.group_bindings = {}
        self.group_member_chats = {}
        self.provider_config_epoch = 0
        self.provider_entry_unsubscribers = {}
        self.unavailable_provider_entries = set()
        self.selected_provider_entry_id = None
        self.provider_refresh_task = None
        self.transport_errors = {}
        self._unsubscribe_provider_changes = lambda: None
        self._closed = False

    async def setup(self):
        if self.maintenance:
            async_register_commands(self.hass)
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.close)
            return
        await self.inbox.load()
        self.dedupe_order = await self.store.async_load() or []
        self.dedupe_order = self.dedupe_order[-2000:]
        self.dedupe = set(self.dedupe_order)
        self.bindings = await self.bindings_store.async_load() or {}
        self.group_bindings = await self.group_bindings_store.async_load() or {}
        async_register_commands(self.hass)
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.close)
        self._unsubscribe_provider_changes = async_dispatcher_connect(
            self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._provider_entry_changed)
        provider_entries = self.hass.config_entries.async_entries(PROVIDER_DOMAIN)
        selected_provider = self._effective_provider_entry(provider_entries)
        self.selected_provider_entry_id = (
            getattr(selected_provider, "entry_id", None) if selected_provider else None
        )
        for entry in provider_entries:
            self._observe_provider_entry(entry)

    @staticmethod
    def _effective_provider_entry(entries):
        from homeassistant.components.habab_home.effective_provider import effective_provider_entry

        return effective_provider_entry(entries)

    @callback
    def _observe_provider_entry(self, entry):
        entry_id = getattr(entry, "entry_id", None)
        add_listener = getattr(entry, "add_update_listener", None)
        if (not isinstance(entry_id, str) or entry_id in self.provider_entry_unsubscribers
                or not callable(add_listener)):
            return
        unsubscribe = add_listener(self._provider_entry_updated)
        if callable(unsubscribe):
            self.provider_entry_unsubscribers[entry_id] = unsubscribe

    @callback
    def _provider_entry_changed(self, change, entry):
        if getattr(entry, "domain", None) != PROVIDER_DOMAIN:
            return
        entry_id = getattr(entry, "entry_id", None)
        state = getattr(getattr(entry, "state", None), "value", None)
        if change == ConfigEntryChange.REMOVED:
            unsubscribe = self.provider_entry_unsubscribers.pop(entry_id, None)
            if unsubscribe:
                unsubscribe()
        if state == "loaded":
            self._observe_provider_entry(entry)
            selected = self._effective_provider_entry(
                self.hass.config_entries.async_entries(PROVIDER_DOMAIN)
            )
            selected_id = getattr(selected, "entry_id", None) if selected else None
            if self.unavailable_provider_entries:
                if selected_id in self.unavailable_provider_entries:
                    self.unavailable_provider_entries.discard(selected_id)
                elif (selected is not None
                      and getattr(selected, "options", {}).get("assistant_selected")):
                    self.unavailable_provider_entries.clear()
                else:
                    return
            self.selected_provider_entry_id = selected_id
            self._provider_config_changed()
            return

        relay_in_use = any(
            state.get("transport_identity", (None,))[0] == "habab_relay"
            for state in self.entries.values()
        )
        if (entry_id == self.selected_provider_entry_id
                and (entry.data.get("provider") == "habab" or relay_in_use)
                and isinstance(entry_id, str)):
            self.unavailable_provider_entries.add(entry_id)
            self._provider_config_changed()

    async def _provider_entry_updated(self, _hass, _entry):
        self._provider_config_changed()

    @callback
    def _provider_config_changed(self):
        if self.maintenance:
            return
        self.provider_config_epoch += 1
        if (self._closed or self.provider_refresh_task
                and not self.provider_refresh_task.done()):
            return
        self.provider_refresh_task = self.hass.async_create_task(
            self._reload_telegram_for_provider_change())

    async def _reload_telegram_for_provider_change(self):
        if self.maintenance:
            return
        epoch = self.provider_config_epoch
        try:
            await asyncio.sleep(0.2)
            epoch = self.provider_config_epoch
            if self.unavailable_provider_entries:
                for entry_id, state in tuple(self.entries.items()):
                    if (self.provider_config_epoch != epoch):
                        break
                    identity = state.get("transport_identity")
                    if not identity or identity[0] == "direct_proxy":
                        continue
                    self.transport_errors[entry_id] = "provider_unavailable"
                    await self.close_entry(entry_id)
            for entry_id, state in tuple(self.entries.items()):
                if self.provider_config_epoch != epoch:
                    break
                entry = next((item for item in self.hass.config_entries.async_entries(DOMAIN)
                              if item.entry_id == entry_id), None)
                if entry is None:
                    continue
                try:
                    identity = telegram_transport_identity(
                        self.hass, entry.data.get("proxy_url"))
                except TransportError as error:
                    self.transport_errors[entry_id] = error.code
                    await self.close_entry(entry_id)
                    continue
                if identity == state.get("transport_identity"):
                    continue
                try:
                    reloaded = await self.hass.config_entries.async_reload(entry_id)
                except Exception:
                    reloaded = False
                if not reloaded:
                    self.transport_errors[entry_id] = "transport_reload_failed"
                    await self.close_entry(entry_id)
            if not self.unavailable_provider_entries:
                for entry in self.hass.config_entries.async_entries(DOMAIN):
                    if self.provider_config_epoch != epoch:
                        break
                    entry_id = entry.entry_id
                    if (entry_id in self.entries
                            or self.transport_errors.get(entry_id) != "provider_unavailable"):
                        continue
                    try:
                        reloaded = await self.hass.config_entries.async_reload(entry_id)
                    except Exception:
                        reloaded = False
                    if not reloaded:
                        self.transport_errors[entry_id] = "transport_reload_failed"
        finally:
            self.provider_refresh_task = None
            if not self._closed and epoch != self.provider_config_epoch:
                self.provider_refresh_task = self.hass.async_create_task(
                    self._reload_telegram_for_provider_change())

    def entry_for_owner(self, owner):
        return next((entry for entry in self.hass.config_entries.async_entries(DOMAIN)
                     if entry.data.get("owner_id") == owner), None)

    async def status(self, owner):
        if self.maintenance:
            return {"phase": "error", "error": "telegram_maintenance", "maintenance": True,
                    "group": {"phase": "error", "error": "telegram_maintenance"}}
        entry = self.entry_for_owner(owner)
        if not entry:
            return {"phase": "not_configured", "group": {"phase": "not_configured"}}
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
            if (not self.transport_errors.get(entry.entry_id)
                    and state is not None and not state.get("error")):
                state["error"] = "worker_not_ready"
        else:
            if not pair or time.monotonic() > pair["expires"]:
                await self.pair(owner)
                pair = self.pairing[entry.entry_id]
            phase = "pairing"
        result = {"phase": phase, "inbox": self.inbox.counts(entry, self._waiting_payload(entry))}
        result["group"] = await self.group_status(owner, entry)
        if state and state.get("username"):
            result["bot_username"] = state["username"]
        if pair and phase == "pairing":
            result["pairing_url"] = pair["url"]
        if state and state.get("error"):
            result["error"] = state["error"]
        elif self.transport_errors.get(entry.entry_id):
            result["error"] = self.transport_errors[entry.entry_id]
        return result

    async def recover_inbox(self, owner, recovery_id):
        if self.maintenance:
            raise ValueError("telegram_maintenance")
        if not await self._owner_is_active_admin(owner):
            raise ValueError("not_allowed")
        entry = self.entry_for_owner(owner)
        if entry is None:
            raise ValueError("not_configured")
        active_turns = (task for key, task in self.turn_tasks.items()
                        if key == entry.entry_id or key.startswith(entry.entry_id + ":"))
        if any(not task.done() for task in (*active_turns, *self.control_tasks.get(entry.entry_id, ()))):
            raise ValueError("telegram_recovery_unavailable")
        discarded = await self.inbox.recover(
            entry, recovery_id, lambda: self._waiting_payload(entry))
        scope = self.inbox.scope(entry)
        self.inbox_dispatched = {key for key in self.inbox_dispatched
                                 if not key.startswith(scope + ":")}
        await self._retry_waiting_update(entry)
        self._pump_inbox(entry)
        return {"recovered": True, "discarded": discarded}

    async def configure(self, owner, token, proxy_url=""):
        if self.maintenance:
            raise ValueError("telegram_maintenance")
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
        if self.maintenance:
            raise ValueError("telegram_maintenance")
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

    async def group_pair(self, owner):
        if self.maintenance:
            raise ValueError("telegram_maintenance")
        """Issue a one-time group-start link to the already paired owner."""
        entry = self.entry_for_owner(owner)
        if not entry:
            raise ValueError("not_configured")
        lock = self.pair_locks.setdefault(entry.entry_id, asyncio.Lock())
        async with lock:
            if not await self._owner_is_active_admin(owner):
                raise ValueError("not_allowed")
            candidate_task = self.group_candidate_tasks.pop(entry.entry_id, None)
            if candidate_task and not candidate_task.done():
                candidate_task.cancel()
                await asyncio.gather(candidate_task, return_exceptions=True)
            state = self.entries.get(entry.entry_id, {})
            binding = self.bindings.get(entry.entry_id)
            if (not state.get("ready") or not binding
                    or state.get("chat_id") != binding.get("chat_id")
                    or state.get("user_id") != binding.get("user_id")
                    or binding.get("bot_id") != entry.data.get("bot_id")):
                raise ValueError("owner_pairing_required")
            nonce = secrets.token_urlsafe(24)
            candidate = {
                "hash": hashlib.sha256(nonce.encode()).hexdigest(),
                "expires": time.monotonic() + GROUP_PAIR_TTL,
                "owner_user_id": binding["user_id"],
            }
            self.group_pairing[entry.entry_id] = candidate
            self.group_candidates.pop(entry.entry_id, None)
            username = state.get("username", entry.data["bot_username"])
            return {"group_pairing_url": f"https://t.me/{username}?startgroup={nonce}"}

    async def group_status(self, owner, entry=None):
        entry = entry or self.entry_for_owner(owner)
        if entry is None:
            return {"phase": "not_configured"}
        candidate = self.group_candidates.get(entry.entry_id)
        if candidate and time.monotonic() > candidate.get("expires", 0):
            self.group_candidates.pop(entry.entry_id, None)
            candidate = None
        binding = self.group_bindings.get(entry.entry_id)
        if binding:
            state = self.entries.get(entry.entry_id, {})
            if binding.get("bot_id") != entry.data.get("bot_id"):
                return {"phase": "error", "error": "bot_changed"}
            if str(binding.get("owner_user_id")) != str(
                    self.bindings.get(entry.entry_id, {}).get("user_id")):
                return {"phase": "error", "error": "owner_pairing_changed"}
            if not state.get("ready"):
                return {"phase": "error", "error": "worker_not_ready"}
            admin = await self._bot_is_admin(entry, int(binding["chat_id"]))
            if admin is None:
                result = {"phase": "error", "error": "verification_unavailable"}
            elif not admin:
                result = {"phase": "awaiting_admin", "title": binding.get("title", ""),
                          "error": "bot_not_admin"}
            else:
                result = {"phase": "connected", "title": binding.get("title", "")}
            pairing = self.group_pairing.get(entry.entry_id)
            if pairing and time.monotonic() <= pairing["expires"]:
                result["pairing_pending"] = True
            if candidate:
                result["pending_phase"] = "awaiting_admin"
                result["pending_error"] = candidate.get("error", "bot_not_admin")
                result["pending_title"] = candidate.get("title", "")
            return result
        if candidate:
            return {"phase": "awaiting_admin", "title": candidate.get("title", ""),
                    "error": candidate.get("error", "bot_not_admin")}
        pairing = self.group_pairing.get(entry.entry_id)
        if pairing and time.monotonic() <= pairing["expires"]:
            return {"phase": "pairing"}
        return {"phase": "not_configured"}

    async def unlink_group(self, owner):
        if self.maintenance:
            raise ValueError("telegram_maintenance")
        if not await self._owner_is_active_admin(owner):
            raise ValueError("not_allowed")
        entry = self.entry_for_owner(owner)
        if entry:
            lock = self.pair_locks.setdefault(entry.entry_id, asyncio.Lock())
            async with lock:
                if not await self._owner_is_active_admin(owner):
                    raise ValueError("not_allowed")
                self.group_pairing.pop(entry.entry_id, None)
                self.group_candidates.pop(entry.entry_id, None)
                await self._clear_group_binding(entry)
        return {"unlinked": True}

    async def _owner_is_active_admin(self, owner):
        user = await self.hass.auth.async_get_user(owner)
        return bool(user and user.is_active and user.is_admin)

    async def notify_setup_complete(self, origin, domain):
        """Send a completion only to the exact currently bound private origin."""
        if self.maintenance:
            return False
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
        if (not state or not state.get("ready")
                or entry.data.get("bot_id") != origin_bot_id
                or not isinstance(expected_bot, str) or not expected_bot
                or str(state.get("username", "")).casefold() != expected_bot.casefold()):
            return False
        member_origin = type(origin.get("user_id")) is int and isinstance(origin.get("group_id"), str)
        group_binding = self.group_bindings.get(entry_id) if member_origin else None
        if member_origin:
            if (not group_binding or group_binding.get("chat_id") != origin["group_id"]
                    or group_binding.get("bot_id") != origin_bot_id
                    or (await self._is_group_member(entry, int(origin["group_id"]),
                                                        origin["user_id"], group_binding)) is not True):
                return False
        elif (not binding or str(binding.get("chat_id")) != chat_id
              or state.get("chat_id") != chat_id
              or state.get("user_id") != binding.get("user_id")
              or binding.get("bot_id") != origin_bot_id
              or binding.get("bot_id") != entry.data.get("bot_id")):
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
                or self.entry_for_owner(owner).entry_id != entry_id):
            return False
        if member_origin:
            if (self.group_bindings.get(entry_id) != group_binding
                    or (await self._is_group_member(entry, int(origin["group_id"]),
                                                        origin["user_id"], group_binding)) is not True):
                return False
        elif self.bindings.get(entry_id) != binding:
            return False
        return True

    async def _retry_setup_notifications(self):
        if self.maintenance:
            return
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

    async def get_chat_member(self, entry_id, chat_id, user_id):
        """Fail-closed Bot API membership lookup through the existing worker."""
        if self.maintenance:
            return None
        if (type(chat_id) is not int or type(user_id) is not int
                or not 0 < user_id <= 2**53 - 1 or not -2**53 < chat_id < 0):
            return None
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending_members[request_id] = (entry_id, future)
        try:
            if not await self._write(entry_id, {"type": "get_chat_member", "chat_id": chat_id,
                                                "user_id": user_id, "request_id": request_id}):
                return None
            return await asyncio.wait_for(future, timeout=8)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        finally:
            self.pending_members.pop(request_id, None)

    async def _clear_group_binding(self, entry):
        """Revoke all known group member turns and histories, preserving owner's DM."""
        entry_id = entry.entry_id
        binding = self.group_bindings.pop(entry_id, None)
        self.group_member_chats.pop(entry_id, None)
        if binding:
            await self.group_bindings_store.async_save(self.group_bindings)
        if binding is None:
            return
        owner = entry.data["owner_id"]
        private_owner = self.bindings.get(entry_id, {}).get("chat_id")
        assistant = self.hass.data.get("habab_home", {}).get("assistant")
        if assistant is None:
            return
        prefix = f"{owner}:{entry_id}:"
        member_chats = [key[len(prefix):] for key in list(assistant.telegram_sessions)
                        if key.startswith(prefix) and key[len(prefix):] != str(private_owner)]
        for chat_id in member_chats:
            task = self.turn_tasks.get(self._turn_key(entry_id, chat_id))
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await assistant.telegram_forget(owner, entry_id, chat_id)

    def _turn_key(self, entry_id, chat_id):
        state = self.entries.get(entry_id, {})
        if chat_id is None or str(chat_id) == str(state.get("chat_id")):
            return entry_id
        return f"{entry_id}:{chat_id}"

    def _advance_cancel_epoch(self, entry, msg):
        """Invalidate earlier messages even while authorization is awaiting the Bot API."""
        chat_id = str(msg["chat_id"])
        epochs = self.cancel_epochs.setdefault(entry.entry_id, {})
        epochs[chat_id] = epochs.get(chat_id, 0) + 1
        cutoff = int(msg["message_id"])
        self.cancelled_through.setdefault(entry.entry_id, {})[chat_id] = max(
            cutoff, self.cancelled_through.get(entry.entry_id, {}).get(chat_id, -1))
        msg["_habab_cancel_registered"] = True
        msg["_habab_cancel_epoch"] = epochs[chat_id]

    def _message_epoch_is_current(self, entry_id, chat_id, epoch):
        return self.cancel_epochs.get(entry_id, {}).get(str(chat_id), 0) == epoch

    async def _bot_is_admin(self, entry, chat_id):
        member = await self.get_chat_member(entry.entry_id, chat_id, entry.data["bot_id"])
        if member is None:
            return None
        return member.get("status") in ("administrator", "creator")

    async def _group_access_context_is_current(self, entry, owner, binding, state):
        if not await self._owner_is_active_admin(owner):
            return False
        owner_binding = self.bindings.get(entry.entry_id)
        return bool(
            self.entry_for_owner(owner) is entry
            and self.entries.get(entry.entry_id) is state
            and state.get("ready")
            and self.group_bindings.get(entry.entry_id) == binding
            and binding.get("bot_id") == entry.data.get("bot_id")
            and owner_binding
            and str(binding.get("owner_user_id")) == str(owner_binding.get("user_id"))
        )

    async def _is_group_member(self, entry, chat_id, user_id, expected_binding=None) -> bool | None:
        """Return True/False for known access, None for an unavailable lookup."""
        owner = entry.data["owner_id"]
        binding = self.group_bindings.get(entry.entry_id)
        if (not binding or (expected_binding is not None and binding != expected_binding)
                or binding.get("bot_id") != entry.data.get("bot_id")
                or str(binding.get("owner_user_id")) != str(
                    self.bindings.get(entry.entry_id, {}).get("user_id"))):
            return False
        binding_snapshot = dict(binding)
        state = self.entries.get(entry.entry_id)
        if (not state or not state.get("ready")
                or not await self._group_access_context_is_current(
                    entry, owner, binding_snapshot, state)):
            return False
        bot_is_admin = await self._bot_is_admin(
            entry, int(binding_snapshot["chat_id"]))
        context_current = await self._group_access_context_is_current(
            entry, owner, binding_snapshot, state)
        if not context_current or bot_is_admin is False:
            return False
        if bot_is_admin is None:
            return None
        member = await self.get_chat_member(
            entry.entry_id, int(binding_snapshot["chat_id"]), user_id)
        if not await self._group_access_context_is_current(
                entry, owner, binding_snapshot, state):
            return False
        if member is None:
            return None
        return bool(member.get("is_member") is True
                    and member.get("status") in
                    ("creator", "administrator", "member", "restricted"))

    async def _revoke_group_member(self, entry, chat_id, notify=False, cancel_turn=True,
                                  group_chat=False, preserve_history=False):
        # Group sessions are shared. Losing one participant's membership must not
        # erase that shared history or send a member-specific notice to everyone.
        if group_chat:
            return
        key = self._turn_key(entry.entry_id, chat_id)
        task = self.turn_tasks.get(key)
        if cancel_turn and task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if preserve_history:
            # An unavailable roster check still stops the turn, but is not
            # evidence that this participant left the group.
            return
        self.group_member_chats.setdefault(entry.entry_id, {}).pop(str(chat_id), None)
        assistant = self.hass.data.get("habab_home", {}).get("assistant")
        if assistant is not None:
            await assistant.telegram_forget(entry.data["owner_id"], entry.entry_id, str(chat_id))
        if notify:
            await self.send(entry.entry_id, chat_id,
                            "Доступ к помощнику отозван: вас больше нет в группе дома.")

    async def _monitor_group_member(self, entry, chat_id, user_id, binding, turn):
        while not turn.done():
            await asyncio.sleep(5)
            if turn.done():
                return
            allowed = await self._is_group_member(entry, int(binding["chat_id"]), user_id, binding)
            if allowed is not True:
                is_group_chat = str(chat_id) == binding.get("chat_id")
                await self._revoke_group_member(
                    entry, chat_id, notify=not is_group_chat, cancel_turn=False,
                    group_chat=is_group_chat, preserve_history=allowed is None)
                if not turn.done():
                    turn.cancel()
                return

    async def member_origin(self, owner, entry_id, chat_id, session_id):
        """Resolve an exact member DM only after a fresh owner/bot/roster check."""
        if not await self._owner_is_active_admin(owner):
            return None
        entry = self.entry_for_owner(owner)
        if entry is None or entry.entry_id != entry_id:
            return None
        state = self.entries.get(entry_id)
        binding = self.group_bindings.get(entry_id)
        dm_chat = str(chat_id)
        if (not state or not state.get("ready") or not binding
                or binding.get("bot_id") != entry.data.get("bot_id")
                or not dm_chat.isdigit() or len(dm_chat) > 32):
            return None
        assistant = self.hass.data.get("habab_home", {}).get("assistant")
        if (assistant is None
                or assistant.telegram_sessions.get(
                    f"{owner}:{entry_id}:{dm_chat}") != session_id):
            return None
        user_id = self.group_member_chats.get(entry_id, {}).get(dm_chat)
        if user_id is None:
            try:
                user_id = int(dm_chat)
            except ValueError:
                return None
        if (await self._is_group_member(entry, int(binding["chat_id"]), int(user_id), binding)) is not True:
            return None
        if (self.group_bindings.get(entry_id) != binding
                or self.entry_for_owner(owner) is not entry
                or not await self._owner_is_active_admin(owner)):
            return None
        return {"channel": "telegram", "owner": owner, "entry_id": entry_id,
                "chat_id": dm_chat, "bot_id": binding["bot_id"],
                "group_id": binding["chat_id"], "user_id": int(user_id)}

    async def _turn_origin_is_current(self, entry, chat_id, user_id, group_binding,
                                      assistant, session_id=None, group_chat=False):
        owner = entry.data["owner_id"]
        if (not await self._owner_is_active_admin(owner)
                or self.entry_for_owner(owner) is not entry):
            return False
        if assistant is not None and session_id is not None:
            if assistant.telegram_sessions.get(
                    f"{owner}:{entry.entry_id}:{chat_id}") != session_id:
                return False
        if group_binding is not None:
            if group_chat and str(chat_id) != group_binding.get("chat_id"):
                return False
            return await self._is_group_member(
                entry, int(group_binding["chat_id"]), user_id, group_binding)
        binding = self.bindings.get(entry.entry_id)
        state = self.entries.get(entry.entry_id)
        return bool(
            binding and state and state.get("ready")
            and entry.data.get("bot_id") == binding.get("bot_id")
            and str(binding.get("chat_id")) == str(chat_id)
            and str(binding.get("user_id")) == str(user_id)
            and str(state.get("chat_id")) == str(chat_id)
            and str(state.get("user_id")) == str(user_id)
        )

    async def _turn_can_send(self, entry, chat_id, user_id, group_binding, assistant,
                             session_id, cancel_epoch, group_chat=False):
        """Check cancellation around ACL lookup; an external send cannot be atomic."""
        await self._wait_for_group_cancel_checks(entry.entry_id, chat_id, group_chat)
        if not self._message_epoch_is_current(entry.entry_id, chat_id, cancel_epoch):
            return False
        allowed = await self._turn_origin_is_current(
            entry, chat_id, user_id, group_binding, assistant, session_id,
            group_chat=group_chat)
        if allowed is not True:
            return allowed
        if group_chat:
            state = self.entries.get(entry.entry_id)
            if (group_binding is None or str(chat_id) != group_binding.get("chat_id")
                    or not state or not await self._group_access_context_is_current(
                        entry, entry.data["owner_id"], group_binding, state)):
                return False
        await self._wait_for_group_cancel_checks(entry.entry_id, chat_id, group_chat)
        return self._message_epoch_is_current(entry.entry_id, chat_id, cancel_epoch)

    async def _wait_for_group_cancel_checks(self, entry_id, chat_id, group_chat):
        if not group_chat:
            return
        pending = tuple(self.pending_group_cancels.get(entry_id, {}).get(str(chat_id), ()))
        if pending:
            await asyncio.gather(*(asyncio.shield(check) for check in pending))

    async def _claim_group_start(self, entry, msg):
        if (msg.get("chat_type") not in ("group", "supergroup")
                or any(type(msg.get(field)) is not int for field in ("chat_id", "user_id", "message_id"))):
            return False
        text = msg.get("text", "").strip()
        username = self.entries.get(entry.entry_id, {}).get("username", "")
        command = f"/start@{username}" if username else "/start"
        if not (text.startswith(command + " ") or text.startswith("/start ")):
            return False
        supplied = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) == 2 else ""
        pairing = self.group_pairing.get(entry.entry_id)
        owner_binding = self.bindings.get(entry.entry_id)
        if (not pairing or time.monotonic() > pairing["expires"]
                or str(msg["user_id"]) != str(pairing["owner_user_id"])
                or not owner_binding
                or str(owner_binding.get("user_id")) != str(pairing["owner_user_id"])
                or not await self._owner_is_active_admin(entry.data["owner_id"])
                or not secrets.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), pairing["hash"])):
            return False
        state = self.entries.get(entry.entry_id)
        if not state or not state.get("ready"):
            return False
        candidate = {"chat_id": str(msg["chat_id"]),
                     "title": str(msg.get("chat_title") or "Группа дома")[:128],
                     "bot_id": entry.data["bot_id"], "owner_user_id": msg["user_id"],
                     "expires": pairing["expires"]}
        candidate["_pairing"] = pairing
        candidate["_owner_binding"] = owner_binding
        lock = self.pair_locks.setdefault(entry.entry_id, asyncio.Lock())
        async with lock:
            if (self.group_pairing.get(entry.entry_id) is not pairing
                    or time.monotonic() > pairing["expires"]
                    or self.bindings.get(entry.entry_id) != owner_binding
                    or not await self._owner_is_active_admin(entry.data["owner_id"])):
                return True
            self.group_candidates[entry.entry_id] = candidate
        await self._finish_group_candidate(entry, candidate,
                                           announce_chat_id=msg["chat_id"])
        return True

    async def _finish_group_candidate(self, entry, candidate, announce_chat_id=None):
        """Verify a pending group's bot rights, then atomically replace its binding."""
        entry_id = entry.entry_id
        pairing = candidate.get("_pairing")
        if (self.group_candidates.get(entry_id) is not candidate
                or self.group_pairing.get(entry_id) is not pairing
                or not pairing or time.monotonic() > pairing["expires"]):
            return False
        admin = await self._bot_is_admin(entry, int(candidate["chat_id"]))
        lock = self.pair_locks.setdefault(entry_id, asyncio.Lock())
        async with lock:
            state = self.entries.get(entry_id, {})
            owner = entry.data["owner_id"]
            owner_binding = candidate.get("_owner_binding")
            current = (self.group_candidates.get(entry_id) is candidate
                       and self.group_pairing.get(entry_id) is pairing
                       and time.monotonic() <= pairing["expires"]
                       and self.bindings.get(entry_id) == owner_binding
                       and owner_binding is not None
                       and str(owner_binding.get("user_id")) == str(candidate.get("owner_user_id"))
                       and candidate.get("bot_id") == entry.data.get("bot_id")
                       and state.get("ready")
                       and await self._owner_is_active_admin(owner))
            if not current:
                if self.group_candidates.get(entry_id) is candidate:
                    self.group_candidates.pop(entry_id, None)
                return False
            if admin is not True:
                candidate["error"] = "bot_not_admin" if admin is False else "verification_unavailable"
                if announce_chat_id is not None:
                    await self.send(entry_id, announce_chat_id,
                                    "Для проверки участников дайте боту права администратора.")
                return False

            old = self.group_bindings.get(entry_id)
            if old and old.get("chat_id") != candidate["chat_id"]:
                await self._clear_group_binding(entry)
            binding = {key: candidate[key] for key in
                       ("chat_id", "title", "bot_id", "owner_user_id")}
            self.group_bindings[entry_id] = binding
            await self.group_bindings_store.async_save(self.group_bindings)
            self.group_pairing.pop(entry_id, None)
            self.group_candidates.pop(entry_id, None)
            notify_chat = (announce_chat_id if announce_chat_id is not None else
                           owner_binding.get("chat_id"))
            if notify_chat is not None:
                await self.send(entry_id, notify_chat,
                                "Группа дома подключена. Участники могут писать помощнику "
                                "в личные сообщения и в эту группу; переписка и ответы "
                                "в группе видны всем её участникам.")
            return True

    def _schedule_candidate_verification(self, entry, candidate):
        entry_id = entry.entry_id
        current = self.group_candidate_tasks.get(entry_id)
        if current and not current.done():
            return
        task = self.hass.async_create_task(self._finish_group_candidate(entry, candidate))
        self.group_candidate_tasks[entry_id] = task
        tasks = self.control_tasks.setdefault(entry_id, set())
        tasks.add(task)

        def done(completed):
            tasks.discard(completed)
            if self.group_candidate_tasks.get(entry_id) is completed:
                self.group_candidate_tasks.pop(entry_id, None)

        task.add_done_callback(done)

    def _schedule_control(self, entry, msg):
        entry_id = entry.entry_id
        chat_id = str(msg.get("chat_id"))
        lanes = self.control_lanes.setdefault(entry_id, {})
        current = lanes.get(chat_id)
        if current and not current.done():
            return
        if len(lanes) >= self.CONTROL_TASK_LIMIT:
            return
        group_cancel_check = (
            msg.get("text", "").strip() == "/cancel"
            and msg.get("chat_type") in ("group", "supergroup")
            and (binding := self.group_bindings.get(entry_id)) is not None
            and str(msg.get("chat_id")) == binding.get("chat_id")
        )
        check = None
        if group_cancel_check:
            check = asyncio.get_running_loop().create_future()
            self.pending_group_cancels.setdefault(entry_id, {}).setdefault(chat_id, set()).add(check)
        task = self.hass.async_create_task(self._handle_control(entry, msg))
        lanes[chat_id] = task
        tasks = self.control_tasks.setdefault(entry_id, set())
        tasks.add(task)

        def done(completed):
            tasks.discard(completed)
            if lanes.get(chat_id) is completed:
                lanes.pop(chat_id, None)
            if check is not None:
                pending = self.pending_group_cancels.get(entry_id, {}).get(chat_id, set())
                pending.discard(check)
                if not pending:
                    self.pending_group_cancels.get(entry_id, {}).pop(chat_id, None)
                if not check.done():
                    check.set_result(None)

        task.add_done_callback(done)
        task.add_done_callback(lambda _completed: self._pump_inbox(entry))
        return task

    async def async_setup_entry(self, entry):
        if self.maintenance:
            return
        proxy = entry.data.get("proxy_url")
        try:
            route_identity = telegram_transport_identity(self.hass, proxy)
            transport, _provider_id = await resolve_telegram_transport(
                self.hass, async_get_clientsession(self.hass), proxy)
            if route_identity != telegram_transport_identity(self.hass, proxy):
                raise TransportError("selected_provider_changed")
        except TransportError as error:
            self.transport_errors[entry.entry_id] = error.code
            raise
        env = {k: os.environ[k] for k in ("PATH", "LANG", "TZ", "SSL_CERT_FILE") if k in os.environ}
        env.update({"HOME": "/config/.habab-telegram", "NODE_NO_WARNINGS": "1"})
        env["HABAB_TELEGRAM_MAINTENANCE_FILE"] = maintenance_path(self.hass)
        proxy_env = worker_proxy_env(entry.data.get("proxy_url"))
        if proxy_env:
            env.update(proxy_env)
        proc = await asyncio.create_subprocess_exec(
            "node", "/opt/habab-telegram/worker.mjs", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=env, start_new_session=True,
        )
        proc.stdin.write((json.dumps({
            "type": "configure", "token": entry.data["token"],
            "telegram_transport": transport,
        }) + "\n").encode())
        await proc.stdin.drain()
        binding = self.bindings.get(entry.entry_id, {})
        if binding.get("bot_id") != entry.data.get("bot_id"):
            binding = {}
            self.bindings.pop(entry.entry_id, None)
            await self.bindings_store.async_save(self.bindings)
        group_binding = self.group_bindings.get(entry.entry_id)
        if group_binding and group_binding.get("bot_id") != entry.data.get("bot_id"):
            self.group_pairing.pop(entry.entry_id, None)
            self.group_candidates.pop(entry.entry_id, None)
            await self._clear_group_binding(entry)
        state = {"process": proc, "chat_id": binding.get("chat_id"),
                 "user_id": binding.get("user_id"), "username": entry.data["bot_username"],
                 "transport_identity": route_identity}
        self.transport_errors.pop(entry.entry_id, None)
        self.entries[entry.entry_id] = state
        self.queues[entry.entry_id] = asyncio.Queue(maxsize=5)
        if any(record["scope"] == self.inbox.scope(entry) and record["phase"] == "processing"
               for record in self.inbox.records.values()):
            self.inbox.recovery_scopes.add(self.inbox.scope(entry))
        self._pump_inbox(entry)
        self.tasks[entry.entry_id] = self.hass.async_create_background_task(
            self._reader(entry, state), f"Habab Telegram reader {entry.entry_id}")
        self.consumer_tasks[entry.entry_id] = self.hass.async_create_background_task(
            self._consume(entry), f"Habab Telegram consumer {entry.entry_id}")

    async def _write(self, entry_id, payload):
        if self.maintenance:
            return False
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
        if self.maintenance:
            return False
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
                request_ids.append(request_id)
                if not await self._write(entry_id, {"type": "send", "chat_id": int(chat_id),
                                                    "text": chunk, "request_id": request_id}):
                    self.pending_sends.pop(request_id, None)
                    break
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
        if self.maintenance:
            return False
        try:
            await self._write(entry_id, {"type": "typing", "chat_id": int(chat_id)})
        except (ValueError, BrokenPipeError, ConnectionError):
            return False
        return True

    async def _reader(self, entry, state):
        if self.maintenance:
            return
        try:
            while line := await state["process"].stdout.readline():
                if len(line) > MAX_LINE:
                    state["error"] = "worker_protocol_error"
                    state["ready"] = False
                    return
                try:
                    payload = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    state["error"] = "worker_protocol_error"
                    state["ready"] = False
                    return
                kind = payload.get("type")
                if kind == "ready":
                    state["username"] = payload.get("username", state["username"])
                    state["ready"] = True
                    state.pop("error", None)
                    self._pump_inbox(entry)
                    self.hass.async_create_task(self._retry_setup_notifications())
                elif kind == "member":
                    request_id = payload.get("request_id")
                    pending = self.pending_members.get(request_id) if isinstance(request_id, str) else None
                    if pending and pending[0] == entry.entry_id and not pending[1].done():
                        pending[1].set_result({"status": payload.get("status"),
                                               "is_member": payload.get("is_member") is True})
                elif kind == "error":
                    request_id = payload.get("request_id")
                    if payload.get("operation") == "member" and isinstance(request_id, str):
                        pending = self.pending_members.get(request_id)
                        if pending and pending[0] == entry.entry_id and not pending[1].done():
                            pending[1].set_result(None)
                    pending = self.pending_sends.pop(request_id, None) if isinstance(request_id, str) else None
                    if pending and not pending[1].done():
                        pending[1].set_result(False)
                    if payload.get("fatal") or payload.get("operation") == "poll":
                        state["error"] = "worker_error"
                        if payload.get("fatal"):
                            state["ready"] = False
                elif kind == "sent":
                    request_id = payload.get("request_id")
                    pending = self.pending_sends.pop(request_id, None) if isinstance(request_id, str) else None
                    if pending and not pending[1].done():
                        pending[1].set_result(True)
                elif kind == "member_update":
                    binding = self.group_bindings.get(entry.entry_id)
                    removed = (payload.get("status") in ("left", "kicked")
                               or (payload.get("status") == "restricted"
                                   and payload.get("is_member") is False))
                    if (binding and str(payload.get("chat_id")) == binding.get("chat_id")
                            and payload.get("chat_type") in ("group", "supergroup") and removed
                            and str(payload.get("user_id")) != str(binding.get("owner_user_id"))):
                        task = self.hass.async_create_task(
                            self._revoke_group_member(entry, str(payload.get("user_id")), notify=True))
                        controls = self.control_tasks.setdefault(entry.entry_id, set())
                        controls.add(task)
                        task.add_done_callback(controls.discard)
                elif kind == "bot_member_update":
                    # Do not perform Bot API requests in the stdout reader: their
                    # replies are consumed here. Verify a pending group separately.
                    binding = self.group_bindings.get(entry.entry_id)
                    if binding and str(payload.get("chat_id")) == binding.get("chat_id"):
                        binding["bot_status"] = payload.get("status")
                    candidate = self.group_candidates.get(entry.entry_id)
                    if (candidate and str(payload.get("chat_id")) == candidate.get("chat_id")
                            and payload.get("status") in ("administrator", "creator")):
                        self._schedule_candidate_verification(entry, candidate)
                elif kind == "message":
                    await self._receive_update(entry, payload)
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
            state["ready"] = False
        except asyncio.CancelledError:
            raise
        except Exception:
            state["error"] = "worker_error"
            state["ready"] = False

    def _waiting_payload(self, entry):
        waiting = self.waiting_updates.get(entry.entry_id)
        if waiting and waiting["scope"] == self.inbox.scope(entry):
            return waiting["payload"]
        return None

    async def _receive_update(self, entry, payload):
        if self.maintenance:
            return False
        # At capacity, retain one unACKed update while still reading send/member
        # replies. The worker cannot advance its offset or deliver the next one.
        waiting = self._waiting_payload(entry)
        if waiting is not None and waiting != payload:
            _, key, digest = self.inbox.identity(entry, waiting)
            committed = self.inbox.records.get(key)
            if not committed or committed["digest"] != digest:
                raise ValueError("worker_delivery_protocol_error")
            # A new update proves the worker consumed the previous ACK, even
            # if the caller was cancelled before drain returned locally.
            self.waiting_updates.pop(entry.entry_id, None)
        try:
            await self.inbox.accept(entry, payload)
        except (OSError, ValueError) as error:
            if isinstance(error, ValueError) and str(error) != "telegram_inbox_full":
                raise
            if isinstance(error, OSError):
                self.inbox.recovery_scopes.add(self.inbox.scope(entry))
            self.waiting_updates[entry.entry_id] = {
                "scope": self.inbox.scope(entry), "payload": dict(payload),
            }
            return False
        if not await self._write(entry.entry_id, {
            "type": "ack_update", "update_id": payload["update_id"],
        }):
            raise ValueError("worker_delivery_ack_failed")
        self.waiting_updates.pop(entry.entry_id, None)
        self._pump_inbox(entry)
        return True

    async def _retry_waiting_update(self, entry):
        waiting = self._waiting_payload(entry)
        if waiting is not None and entry.entry_id in self.entries:
            await self._receive_update(entry, waiting)

    def _pump_inbox(self, entry):
        if self.maintenance:
            return
        state = self.entries.get(entry.entry_id)
        queue = self.queues.get(entry.entry_id)
        if state is None or queue is None or not state.get("ready"):
            return
        for key, raw in self.inbox.queued(entry):
            if key in self.inbox_dispatched:
                continue
            payload = dict(raw)
            payload["_habab_inbox_key"] = key
            route_control = self._should_route_control(entry, state, payload)
            if not route_control and queue.full():
                continue
            lanes = self.control_lanes.get(entry.entry_id, {})
            lane = lanes.get(str(payload.get("chat_id")))
            if route_control and ((lane and not lane.done()) or len(lanes) >= self.CONTROL_TASK_LIMIT):
                continue
            if payload.get("chat_type") == "private" and payload.get("text", "").strip() == "/cancel":
                self._advance_cancel_epoch(entry, payload)
            payload.setdefault("_habab_cancel_epoch", self.cancel_epochs.get(
                entry.entry_id, {}).get(str(payload.get("chat_id")), 0))
            self.inbox_dispatched.add(key)
            if route_control:
                if self._schedule_control(entry, payload) is None:
                    self.inbox_dispatched.discard(key)
            else:
                if payload.get("chat_type") == "private":
                    self.pending_message_ids.setdefault(entry.entry_id, {}).setdefault(
                        str(payload["chat_id"]), set()).add(payload["message_id"])
                queue.put_nowait(payload)

    async def _begin_incoming(self, msg):
        key = msg.get("_habab_inbox_key")
        return await self.inbox.begin(key) if key else True

    async def _finish_incoming(self, entry, msg):
        key = msg.get("_habab_inbox_key")
        if key:
            await self.inbox.finish(key, msg)
            self.inbox_dispatched.discard(key)
        await self._retry_waiting_update(entry)
        self._pump_inbox(entry)

    def _should_route_control(self, entry, state, msg):
        value = msg.get("text")
        if not isinstance(value, str):
            return False
        text = value.strip()
        chat_type = msg.get("chat_type")
        is_group_message = chat_type in ("group", "supergroup")
        if (text not in ("/help", "/cancel", "/new")
                or (chat_type != "private" and not is_group_message)
                or any(type(msg.get(field)) is not int
                       for field in ("chat_id", "user_id", "message_id"))):
            return False
        if is_group_message:
            binding = self.group_bindings.get(entry.entry_id)
            return bool(binding and str(msg["chat_id"]) == binding.get("chat_id"))
        if text == "/help":
            return False
        key = self._turn_key(entry.entry_id, str(msg["chat_id"]))
        if text == "/new":
            current = self.turn_tasks.get(key)
            if not current or current.done():
                return False
        return True

    async def _handle_control(self, entry, msg):
        if self.maintenance:
            return
        try:
            if await self._begin_incoming(msg):
                await self.handle_one(entry, msg)
                await self._finish_incoming(entry, msg)
            elif key := msg.get("_habab_inbox_key"):
                self.inbox_dispatched.discard(key)
        except OSError:
            self.inbox.recovery_scopes.add(self.inbox.scope(entry))

    async def _claim_message(self, entry, msg):
        """Persist a trusted owner/member update before any command can be queued."""
        if any(type(msg.get(field)) is not int for field in ("chat_id", "user_id", "message_id")):
            return False
        if not isinstance(msg.get("text"), str):
            return False
        chat_type = msg.get("chat_type")
        is_group_message = chat_type in ("group", "supergroup")
        if is_group_message:
            if await self._claim_group_start(entry, msg):
                return False
            # Only the shared conversation controls are accepted in the bound
            # group. Pairing and every other Telegram command remain private.
            if msg["text"].strip() not in ("/help", "/new", "/cancel") \
                    and msg["text"].strip().startswith("/"):
                return False
        elif chat_type != "private":
            return False
        state = self.entries.get(entry.entry_id)
        if not state:
            return False
        chat_id, user_id = str(msg["chat_id"]), str(msg["user_id"])
        if is_group_message:
            binding = self.group_bindings.get(entry.entry_id)
            if (not binding or chat_id != binding.get("chat_id")
                    or (await self._is_group_member(
                        entry, int(binding["chat_id"]), msg["user_id"], binding)) is not True):
                return False
            msg["_habab_member"] = True
            msg["_habab_group_message"] = True
        elif state.get("chat_id") is None:
            pairing = self.pairing.get(entry.entry_id)
            text = msg.get("text", "")
            supplied = text[7:].strip() if text.startswith("/start ") else ""
            if (not pairing or time.monotonic() > pairing["expires"]
                    or not secrets.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), pairing["hash"])):
                await self.send(entry.entry_id, msg["chat_id"], BOT_ONBOARDING_MESSAGE)
                return False
        elif chat_id == state["chat_id"] and user_id == state["user_id"]:
            if not await self._owner_is_active_admin(entry.data["owner_id"]):
                msg["_habab_revoked_owner"] = True
            else:
                msg["_habab_owner"] = True
        else:
            binding = self.group_bindings.get(entry.entry_id)
            if not await self._owner_is_active_admin(entry.data["owner_id"]):
                await self._clear_group_binding(entry)
                return False
            allowed = (await self._is_group_member(entry, int(binding["chat_id"]), msg["user_id"], binding)
                       if binding else False)
            if allowed is True:
                msg["_habab_member"] = True
                self.group_member_chats.setdefault(entry.entry_id, {})[chat_id] = msg["user_id"]
            else:
                assistant = self.hass.data.get("habab_home", {}).get("assistant")
                has_history = bool(assistant and
                                   f"{entry.data['owner_id']}:{entry.entry_id}:{chat_id}"
                                   in assistant.telegram_sessions)
                if has_history:
                    await self._revoke_group_member(entry, chat_id, preserve_history=allowed is None)
                await self.send(entry.entry_id, msg["chat_id"],
                                "Не удалось проверить доступ к группе. Повторите запрос позже."
                                if allowed is None else UNKNOWN_MEMBER_MESSAGE)
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
        msg["_habab_claimed"] = True
        return True

    async def handle_one(self, entry, msg):
        if self.maintenance:
            return
        if msg.get("_habab_handled"):
            return
        state = self.entries.get(entry.entry_id)
        chat_type = msg.get("chat_type")
        is_group_message = chat_type in ("group", "supergroup")
        if (not state or (chat_type != "private" and not is_group_message)):
            return
        if any(not isinstance(msg.get(field), int) for field in ("chat_id", "user_id", "message_id")):
            return
        chat_id, user_id = str(msg.get("chat_id")), str(msg.get("user_id"))
        text = msg.get("text", "").strip()
        epoch = msg.get("_habab_cancel_epoch",
                        self.cancel_epochs.get(entry.entry_id, {}).get(chat_id, 0))
        if not msg.get("_habab_claimed") and not await self._claim_message(entry, msg):
            return
        if is_group_message and not msg.get("_habab_group_message"):
            return
        if text != "/cancel" and not self._message_epoch_is_current(entry.entry_id, chat_id, epoch):
            return
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
            if not self._message_epoch_is_current(entry.entry_id, chat_id, epoch):
                return
            if self.pairing.get(entry.entry_id) is not pairing or time.monotonic() > pairing["expires"]:
                return
            if not msg.get("_habab_claimed") and not await self._claim_message(entry, msg):
                return
            state.update(chat_id=chat_id, user_id=user_id)
            self.bindings[entry.entry_id] = {"chat_id": chat_id, "user_id": user_id,
                                             "bot_id": entry.data.get("bot_id")}
            await self.bindings_store.async_save(self.bindings)
            group_binding = self.group_bindings.get(entry.entry_id)
            if (group_binding and str(group_binding.get("owner_user_id")) != user_id):
                await self._clear_group_binding(entry)
            self.pairing.pop(entry.entry_id, None)
            await self.send(entry.entry_id, chat_id, OWNER_PAIRING_COMPLETE_MESSAGE)
            return
        is_owner = (chat_id == state.get("chat_id") and user_id == state.get("user_id"))
        current_group_binding = None if is_owner else self.group_bindings.get(entry.entry_id)
        group_binding = (dict(current_group_binding) if current_group_binding else None)
        if is_owner:
            if not await self._owner_is_active_admin(entry.data["owner_id"]):
                await self._clear_binding(entry)
                await self.send(entry.entry_id, chat_id, "Доступ отозван.")
                return
        else:
            allowed = (await self._is_group_member(entry, int(group_binding["chat_id"]), int(user_id), group_binding)
                       if group_binding and (not is_group_message or chat_id == group_binding.get("chat_id")) else False)
            if allowed is not True:
                await self._revoke_group_member(entry, chat_id, notify=not is_group_message,
                                                group_chat=is_group_message, preserve_history=allowed is None)
                return
        if text != "/cancel" and not self._message_epoch_is_current(entry.entry_id, chat_id, epoch):
            return
        msg["_habab_handled"] = True
        if len(text) > 4000:
            await self.send(entry.entry_id, chat_id, "Сообщение слишком длинное (максимум 4000 символов).")
            return
        if text == "/help":
            commands = (
                "Команды работают в личных сообщениях и в подключённой группе:\n"
                "/new — начать новый диалог\n"
                "/cancel — остановить текущую задачу\n"
                "История и ответы в группе видны всем её участникам."
            )
            if is_owner:
                if self.group_bindings.get(entry.entry_id):
                    guidance = (
                        "Группа дома подключена. Участники могут писать боту в личные "
                        "сообщения или в эту группу. Переписка и ответы в группе общие "
                        "и видны всем её участникам."
                    )
                else:
                    guidance = OWNER_PAIRING_COMPLETE_MESSAGE
            else:
                guidance = (
                    "Пишите боту в личные сообщения или в привязанную группу дома. "
                    "Переписка и ответы в группе общие и видны всем её участникам. "
                    "Доступом к дому управляет владелец."
                )
            await self.send(entry.entry_id, chat_id, f"{guidance}\n\n{commands}")
        elif text == "/new":
            key = self._turn_key(entry.entry_id, chat_id)
            current = self.turn_tasks.get(key)
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
            if not msg.get("_habab_cancel_registered"):
                self._advance_cancel_epoch(entry, msg)
            key = self._turn_key(entry.entry_id, chat_id)
            cutoff = int(msg["message_id"])
            self.cancelled_through.setdefault(entry.entry_id, {})[chat_id] = max(
                cutoff, self.cancelled_through.get(entry.entry_id, {}).get(chat_id, -1))
            queued = any(message_id < cutoff for message_id in
                         self.pending_message_ids.get(entry.entry_id, {}).get(chat_id, set()))
            current = self.turn_tasks.get(key)
            if current and not current.done():
                current.cancel()
                await asyncio.gather(current, return_exceptions=True)
                stopped = True
            elif queued:
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
                key = self._turn_key(entry.entry_id, chat_id)
                task = self.hass.async_create_task(self._run_message(
                    entry, chat_id, int(user_id), text, group_binding, epoch,
                    group_chat=is_group_message))
                self.turn_tasks[key] = task

    async def _run_message(self, entry, chat_id, user_id, text, group_binding=None,
                           cancel_epoch=None, group_chat=False):
        monitor = None
        session_id = None
        assistant = None
        if cancel_epoch is None:
            cancel_epoch = self.cancel_epochs.get(entry.entry_id, {}).get(str(chat_id), 0)
        try:
            assistant = self.hass.data.get("habab_home", {}).get("assistant")
            allowed = await self._turn_origin_is_current(
                    entry, chat_id, user_id, group_binding, assistant,
                    group_chat=group_chat)
            if allowed is not True:
                if group_binding is not None:
                    await self._revoke_group_member(entry, chat_id, cancel_turn=False,
                                                    group_chat=(str(chat_id) ==
                                                                group_binding.get("chat_id")),
                                                    preserve_history=allowed is None)
                return
            if assistant is None:
                if (await self._turn_origin_is_current(
                        entry, chat_id, user_id, group_binding, assistant,
                        group_chat=group_chat)) is True:
                    await self.send(entry.entry_id, chat_id,
                                    "Помощник запускается. Повторите запрос позже.")
                return
            session = await assistant.telegram_session(entry.data["owner_id"], entry.entry_id, chat_id)
            session_id = session["session_id"]
            # Recheck after session loading and again after the typing request.
            allowed = await self._turn_origin_is_current(
                    entry, chat_id, user_id, group_binding, assistant, session_id,
                    group_chat=group_chat)
            if allowed is not True:
                if group_binding is not None:
                    await self._revoke_group_member(entry, chat_id, cancel_turn=False,
                                                    group_chat=(str(chat_id) ==
                                                                group_binding.get("chat_id")),
                                                    preserve_history=allowed is None)
                return
            await self.typing(entry.entry_id, chat_id)
            allowed = await self._turn_origin_is_current(
                    entry, chat_id, user_id, group_binding, assistant, session_id,
                    group_chat=group_chat)
            if allowed is not True:
                if group_binding is not None:
                    await self._revoke_group_member(entry, chat_id, cancel_turn=False,
                                                    group_chat=(str(chat_id) ==
                                                                group_binding.get("chat_id")),
                                                    preserve_history=allowed is None)
                return
            if group_binding is not None:
                monitor = asyncio.create_task(self._monitor_group_member(
                    entry, chat_id, user_id, group_binding, asyncio.current_task()))
            result = await assistant.run(entry.data["owner_id"], "telegram", session_id,
                                         text, entry.entry_id, chat_id)
            # Runtime completion does not preserve authority: verify the exact
            # owner/member binding and session again before sending the answer.
            allowed = await self._turn_can_send(entry, chat_id, user_id, group_binding,
                                         assistant, session_id, cancel_epoch,
                                         group_chat=group_chat)
            if allowed is True:
                # A later cancel cannot retract a Bot API request already in flight.
                await self.send(entry.entry_id, chat_id, result["messages"][-1]["text"])
            elif group_binding is not None:
                await self._revoke_group_member(entry, chat_id, cancel_turn=False,
                                                group_chat=(str(chat_id) ==
                                                            group_binding.get("chat_id")),
                                                preserve_history=allowed is None)
        except asyncio.CancelledError:
            raise
        except Exception:
            allowed = await self._turn_can_send(entry, chat_id, user_id, group_binding,
                                         assistant, session_id, cancel_epoch,
                                         group_chat=group_chat)
            if allowed is True:
                await self.send(entry.entry_id, chat_id,
                                "Не удалось выполнить запрос. Попробуйте позже.")
            elif group_binding is not None:
                await self._revoke_group_member(entry, chat_id, cancel_turn=False,
                                                group_chat=(str(chat_id) ==
                                                            group_binding.get("chat_id")),
                                                preserve_history=allowed is None)
        finally:
            if monitor:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)

    async def _consume(self, entry):
        if self.maintenance:
            return
        queue = self.queues[entry.entry_id]
        while True:
            msg = await queue.get()
            try:
                if await self._begin_incoming(msg):
                    await self._consume_message(entry, msg)
                    await self._finish_incoming(entry, msg)
            except OSError:
                # A failed begin must not run; a failed finish may already have
                # acted. Preserve both for explicit recovery, never auto-retry.
                self.inbox.recovery_scopes.add(self.inbox.scope(entry))
            finally:
                self.pending_message_ids.get(entry.entry_id, {}).get(
                    str(msg.get("chat_id")), set()).discard(msg.get("message_id"))
                queue.task_done()
                key = msg.get("_habab_inbox_key")
                if key and self.inbox.records.get(key, {}).get("phase") != "processing":
                    self.inbox_dispatched.discard(key)
                self._pump_inbox(entry)

    async def _consume_message(self, entry, msg):
        try:
            chat_id = str(msg.get("chat_id"))
            message_id = msg.get("message_id", -1)
            cutoff = self.cancelled_through.get(entry.entry_id, {}).get(chat_id, -1)
            epoch = msg.get("_habab_cancel_epoch",
                            self.cancel_epochs.get(entry.entry_id, {}).get(chat_id, 0))
            if (message_id <= cutoff
                    or not self._message_epoch_is_current(entry.entry_id, chat_id, epoch)):
                return
            if not await self._claim_message(entry, msg):
                return
            await self.handle_one(entry, msg)
            self.pending_message_ids.get(entry.entry_id, {}).get(chat_id, set()).discard(message_id)
            turn = self.turn_tasks.get(self._turn_key(entry.entry_id, chat_id))
            if turn and not turn.done():
                # The reader handles acknowledgments and direct controls independently.
                await asyncio.gather(turn, return_exceptions=True)
        except Exception:
            # Provider and HA errors are intentionally reduced to a stable user message.
            state = self.entries.get(entry.entry_id)
            reply_chat_id = None
            is_group_message = msg.get("chat_type") in ("group", "supergroup")
            if is_group_message:
                binding = self.group_bindings.get(entry.entry_id)
                if (msg.get("_habab_group_message") is True and binding
                        and type(msg.get("chat_id")) is int
                        and type(msg.get("user_id")) is int
                        and str(msg["chat_id"]) == binding.get("chat_id")):
                    try:
                        member_is_current = await self._is_group_member(
                            entry, msg["chat_id"], msg["user_id"], binding)
                    except Exception:
                        member_is_current = False
                    if member_is_current is True:
                        reply_chat_id = msg["chat_id"]
            elif state and state.get("chat_id"):
                reply_chat_id = state["chat_id"]
            if (reply_chat_id
                    and self._message_epoch_is_current(entry.entry_id, chat_id, epoch)):
                await self.send(entry.entry_id, reply_chat_id,
                                "Не удалось обработать сообщение.")

    async def close_entry(self, entry_id):
        state = self.entries.pop(entry_id, None)
        task = self.tasks.pop(entry_id, None)
        consumer = self.consumer_tasks.pop(entry_id, None)
        turns = [self.turn_tasks.pop(key) for key in list(self.turn_tasks)
                 if key == entry_id or key.startswith(entry_id + ":")]
        controls = self.control_tasks.pop(entry_id, set())
        self.control_lanes.pop(entry_id, None)
        for current in (task, consumer, *turns, *controls):
            if current:
                current.cancel()
        await asyncio.gather(
            *(item for item in (task, consumer, *turns, *controls) if item),
            return_exceptions=True,
        )
        for request_id, (send_entry_id, future) in list(self.pending_sends.items()):
            if send_entry_id == entry_id:
                if not future.done():
                    future.set_result(False)
                self.pending_sends.pop(request_id, None)
        for request_id, (member_entry_id, future) in list(self.pending_members.items()):
            if member_entry_id == entry_id:
                if not future.done():
                    future.set_result(None)
                self.pending_members.pop(request_id, None)
        self.queues.pop(entry_id, None)
        self.waiting_updates.pop(entry_id, None)
        self.inbox_dispatched = {key for key in self.inbox_dispatched
                                 if not key.startswith(entry_id + ":")}
        self.pending_message_ids.pop(entry_id, None)
        self.cancelled_through.pop(entry_id, None)
        self.cancel_epochs.pop(entry_id, None)
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
        self.group_pairing.pop(entry_id, None)
        self.group_candidates.pop(entry_id, None)
        self.group_candidate_tasks.pop(entry_id, None)

    async def unlink(self, owner):
        if self.maintenance:
            raise ValueError("telegram_maintenance")
        entry = self.entry_for_owner(owner)
        if entry:
            lock = self.pair_locks.setdefault(entry.entry_id, asyncio.Lock())
            async with lock:
                if not await self._owner_is_active_admin(owner):
                    raise ValueError("not_allowed")
                self.group_pairing.pop(entry.entry_id, None)
                self.group_candidates.pop(entry.entry_id, None)
                await self._clear_group_binding(entry)
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
        self._closed = True
        if self.provider_refresh_task and not self.provider_refresh_task.done():
            self.provider_refresh_task.cancel()
            await asyncio.gather(self.provider_refresh_task, return_exceptions=True)
        self._unsubscribe_provider_changes()
        for unsubscribe in self.provider_entry_unsubscribers.values():
            unsubscribe()
        self.provider_entry_unsubscribers.clear()
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
