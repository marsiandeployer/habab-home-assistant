"""Persist incoming text updates before the worker advances its Telegram offset."""
import asyncio
import hashlib
import json

from homeassistant.helpers.storage import Store


class InboxStore(Store):
    """Acknowledge only an atomic native write verified from disk, not its cache."""

    def __init__(self, hass):
        super().__init__(hass, 1, "habab_telegram.inbox", private=True, atomic_writes=True)

    async def async_save(self, data):
        # Core Store can defer writes during stopping and suppress WriteError.
        await super().async_save(data)
        await self.hass.async_add_executor_job(self._verify_saved, data)

    def _verify_saved(self, expected):
        try:
            with open(self.path, encoding="utf-8") as saved:
                envelope = json.load(saved)
        except json.JSONDecodeError as error:
            raise OSError("telegram_inbox_write_not_verified") from error
        if not isinstance(envelope, dict) or envelope.get("data") != expected:
            raise OSError("telegram_inbox_write_not_verified")


class TelegramInbox:
    MAX_PENDING = 256
    MAX_DONE = 2000

    def __init__(self, store):
        self.store = store
        self.lock = asyncio.Lock()
        self.records = {}
        self.recovery_scopes = set()

    @staticmethod
    def scope(entry):
        return f"{entry.entry_id}:{entry.data['owner_id']}:{entry.data['bot_id']}"

    async def load(self):
        self.records = await self.store.async_load() or {}
        # An interrupted action may already have taken effect. Do not replay it,
        # or later queued instructions, automatically after restart/reload.
        self.recovery_scopes = {
            record["scope"] for record in self.records.values()
            if record["phase"] == "processing"
        }

    def identity(self, entry, payload):
        update_id = payload.get("update_id")
        if type(update_id) is not int or update_id < 0:
            raise ValueError("worker_delivery_protocol_error")
        scope = self.scope(entry)
        key = f"{scope}:{update_id}"
        digest = hashlib.sha256(json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()).hexdigest()
        return scope, key, digest

    async def accept(self, entry, payload):
        scope, key, digest = self.identity(entry, payload)
        async with self.lock:
            existing = self.records.get(key)
            if existing:
                if existing["digest"] != digest:
                    raise ValueError("worker_update_changed")
                return key
            if sum(record["scope"] == scope and record["phase"] != "done"
                   for record in self.records.values()) >= self.MAX_PENDING:
                raise ValueError("telegram_inbox_full")
            records = dict(self.records)
            records[key] = {"scope": scope, "phase": "queued", "digest": digest,
                            "payload": dict(payload)}
            await self.store.async_save(records)
            self.records = records
        return key

    def queued(self, entry):
        scope = self.scope(entry)
        if scope in self.recovery_scopes:
            return []
        return [(key, record["payload"]) for key, record in self.records.items()
                if record["scope"] == scope and record["phase"] == "queued"]

    async def begin(self, key):
        async with self.lock:
            record = self.records.get(key)
            if (not record or record["phase"] != "queued"
                    or record["scope"] in self.recovery_scopes):
                return False
            records = dict(self.records)
            records[key] = {**record, "phase": "processing"}
            await self.store.async_save(records)
            self.records = records
            return True

    async def finish(self, key, message):
        async with self.lock:
            records = dict(self.records)
            current = records[key]
            cancelled = (message.get("_habab_claimed") and
                         message.get("text", "").strip() == "/cancel")
            for record_key, record in tuple(records.items()):
                payload = record.get("payload", {})
                cancelled_queued = (
                    cancelled and record["scope"] == current["scope"]
                    and record["phase"] == "queued"
                    and payload.get("chat_id") == message.get("chat_id")
                    and payload.get("message_id", -1) <= message["message_id"]
                )
                if record_key == key or cancelled_queued:
                    records[record_key] = {name: value for name, value in record.items()
                                           if name != "payload"} | {"phase": "done"}
            self._trim_done(records, current["scope"])
            await self.store.async_save(records)
            self.records = records

    def _trim_done(self, records, scope):
        done = [key for key, record in records.items()
                if record["scope"] == scope and record["phase"] == "done"]
        for key in done[:-self.MAX_DONE]:
            records.pop(key)

    def counts(self, entry, waiting=None):
        scope = self.scope(entry)
        records = [(key, record) for key, record in self.records.items() if record["scope"] == scope]
        if waiting and self.identity(entry, waiting)[1] in self.records:
            waiting = None  # Already committed; only its IPC ACK may remain.
        result = {"queued": sum(record["phase"] == "queued" for _, record in records) + bool(waiting),
                "processing": sum(record["phase"] == "processing" for _, record in records),
                "recovery_required": scope in self.recovery_scopes}
        if result["recovery_required"]:
            snapshot = sorted((key, record["digest"], record["phase"])
                              for key, record in records if record["phase"] != "done")
            if waiting:
                _, key, digest = self.identity(entry, waiting)
                snapshot.append((key, digest, "waiting"))
            result["recovery_id"] = hashlib.sha256(json.dumps(snapshot).encode()).hexdigest()
        return result

    async def recover(self, entry, recovery_id, waiting_payload):
        """Owner-approved discard, never a retry of a possibly executed command."""
        async with self.lock:
            waiting = waiting_payload()
            state = self.counts(entry, waiting)
            if not state["recovery_required"]:
                raise ValueError("telegram_recovery_unavailable")
            if state["recovery_id"] != recovery_id:
                raise ValueError("telegram_recovery_stale")
            scope = self.scope(entry)
            records = dict(self.records)
            discarded = 0
            for key, record in tuple(records.items()):
                if record["scope"] == scope and record["phase"] != "done":
                    records[key] = {name: value for name, value in record.items()
                                    if name != "payload"} | {"phase": "done"}
                    discarded += 1
            if waiting:
                _, key, digest = self.identity(entry, waiting)
                is_new = key not in records
                records[key] = {"scope": scope, "digest": digest, "phase": "done"}
                discarded += is_new
            self._trim_done(records, scope)
            await self.store.async_save(records)
            self.records = records
            self.recovery_scopes.discard(scope)
            return discarded
