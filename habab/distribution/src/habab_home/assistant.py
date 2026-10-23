"""Shared authenticated front door for the web and Telegram setup chats."""

from homeassistant.helpers.storage import Store

from .agent_diagnostics import AgentDiagnostics
from .assist_adapter import AssistAdapter
from .setup_agent import AgentError


class AssistantService:
    """Own channel-specific session maps and delegate to the selected runtime."""

    def __init__(self, hass, runtime):
        self.hass = hass
        self.runtime = runtime
        # Keep the established keys so existing owner histories survive upgrades.
        self.web_store = Store(hass, 1, "habab_home.agent_sessions", private=True)
        self.telegram_store = Store(hass, 1, "habab_home.telegram_sessions", private=True)
        self.web_sessions = {}
        self.telegram_sessions = {}
        self.diagnostics = runtime.diagnostics

    async def setup(self):
        self.web_sessions = await self.web_store.async_load() or {}
        self.telegram_sessions = await self.telegram_store.async_load() or {}

    async def _require_active_admin(self, owner):
        user = await self.hass.auth.async_get_user(owner)
        if not user or not user.is_active or not user.is_admin:
            raise AgentError("not_allowed")

    def _telegram_key(self, owner, entry_id, chat_id):
        return f"{owner}:{entry_id}:{chat_id}"

    async def _session(self, mapping, store, key, title, new=False):
        if self.runtime.lock.locked():
            raise AgentError("busy")
        async with self.runtime.lock:
            await self.runtime.ensure()
            if new or key not in mapping:
                created = await self.runtime.request("POST", "session", {"title": title})
                mapping[key] = created["id"]
                await store.async_save(mapping)
            session_id = mapping[key]
            messages = await self.runtime.request(
                "GET", f"session/{session_id}/message?limit=60")
            return {"session_id": session_id, "messages": self.runtime.visible(messages)}

    async def web_session(self, owner, new=False):
        await self._require_active_admin(owner)
        return await self._session(self.web_sessions, self.web_store, owner,
                                   "Home Assistant", new)

    async def telegram_session(self, owner, entry_id, chat_id, new=False):
        await self._require_active_admin(owner)
        key = self._telegram_key(owner, entry_id, chat_id)
        return await self._session(self.telegram_sessions, self.telegram_store, key,
                                   "Telegram Home Assistant", new)

    async def telegram_reset(self, owner, entry_id, chat_id):
        await self._require_active_admin(owner)
        key = self._telegram_key(owner, entry_id, chat_id)
        if self.runtime.active == (owner, self.telegram_sessions.get(key)):
            raise AgentError("busy")
        return await self._session(self.telegram_sessions, self.telegram_store, key,
                                   "Telegram Home Assistant", new=True)

    async def telegram_forget(self, owner, entry_id, chat_id):
        key = self._telegram_key(owner, entry_id, chat_id)
        session_id = self.telegram_sessions.pop(key, None)
        # Revocation is cleanup, so it must still work after the HA account was
        # disabled. The exact owner+bot+chat mapping scopes any active abort.
        if session_id and self.runtime.active == (owner, session_id):
            await self.runtime.abort(owner, session_id)
        await self.telegram_store.async_save(self.telegram_sessions)
        return session_id

    def _mapped_session(self, owner, channel, session_id, entry_id=None, chat_id=None):
        if channel == "web":
            return self.web_sessions.get(owner) == session_id
        if channel == "telegram" and entry_id is not None and chat_id is not None:
            key = self._telegram_key(owner, entry_id, chat_id)
            return self.telegram_sessions.get(key) == session_id
        return False

    async def run(self, owner, channel, session_id, text, entry_id=None, chat_id=None):
        await self._require_active_admin(owner)
        if not self._mapped_session(owner, channel, session_id, entry_id, chat_id):
            raise AgentError("not_found")
        return await self.runtime.run(owner, session_id, text)

    async def abort(self, owner, channel, session_id, entry_id=None, chat_id=None):
        await self._require_active_admin(owner)
        if not self._mapped_session(owner, channel, session_id, entry_id, chat_id):
            raise AgentError("not_found")
        return await self.runtime.abort(owner, session_id)

    async def diagnostics_for_web(self, owner, session_id):
        await self._require_active_admin(owner)
        if not self._mapped_session(owner, "web", session_id):
            raise AgentError("not_found")
        process = self.runtime.process
        state = "running" if process is not None and process.returncode is None else "stopped"
        return {
            "session_id": session_id,
            "active": self.runtime.active == (owner, session_id),
            "runtime_status": state,
            "error": self.diagnostics.get(owner, session_id),
        }

    async def run_assist(self, entry, chat_log):
        """Preserve native Assist's narrow exposed-tool runtime and permissions."""
        await AssistAdapter(self.hass, entry.entry).complete(chat_log, entry.entity_id)
