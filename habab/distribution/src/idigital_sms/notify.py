"""One fixed-recipient notify entity per I-Digital configuration entry."""

import asyncio
import hashlib
import logging
import math
import os
import time
from typing import Literal, TypedDict, cast
from uuid import UUID, uuid4

from homeassistant.components.notify import NotifyEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .api import IDigitalError, validate_message
from .const import (
    ATTR_MESSAGE_UUID,
    DOMAIN,
    MAX_UNRESOLVED_ATTEMPTS,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Create the single configured SMS notify entity."""
    client = hass.data[DOMAIN][entry.entry_id]["client"]
    store_key = f"{DOMAIN}.send_guard.{entry.entry_id}"
    store = Store[GuardState](
        hass,
        STORAGE_VERSION,
        store_key,
        private=True,
        atomic_writes=True,
    )
    readback_store = Store[GuardState](
        hass,
        STORAGE_VERSION,
        store_key,
        private=True,
    )
    send_guard = PersistentSendGuard(store, readback_store)
    await send_guard.async_load()
    async_add_entities([IDigitalSmsNotifyEntity(client, entry.entry_id, send_guard)])


SendPhase = Literal["reserved", "accepted", "rejected", "unknown"]


class SendAttempt(TypedDict):
    """Persisted minimal record for one provider request."""

    attempt_id: str
    timestamp: float
    digest: str
    phase: SendPhase
    message_uuid: str | None


class GuardState(TypedDict):
    """Persisted rate-limit and idempotency state."""

    attempts: list[SendAttempt]


class PersistentSendGuard:
    """Reserve and persist a send before making its non-idempotent HTTP request."""

    def __init__(
        self,
        store: Store[GuardState],
        readback_store: Store[GuardState],
    ) -> None:
        self._store = store
        self._readback_store = readback_store
        self._lock = asyncio.Lock()
        self._attempts: list[SendAttempt] = []
        self._storage_failed = False

    @property
    def latest_message_uuid(self) -> str | None:
        """Return the newest provider UUID retained in the persisted attempts."""
        for attempt in reversed(self._attempts):
            message_uuid = attempt.get("message_uuid")
            if message_uuid is not None:
                return message_uuid
        return None

    async def async_load(self) -> None:
        """Load state; corrupt or unavailable storage disables sending fail-closed."""
        try:
            store_path = self._readback_store.path
            store_existed = await self._readback_store.hass.async_add_executor_job(
                self._has_store_or_corrupt_backup,
                store_path,
            )
            stored = await self._readback_store.async_load()
        except Exception:
            self._storage_failed = True
            return
        if stored is None:
            if store_existed:
                # Core can quarantine corrupt JSON and return None as if no store existed.
                self._storage_failed = True
                return
            try:
                # Establish and read back an empty baseline before the first permitted send.
                await self._async_save_and_verify()
            except Exception:
                self._storage_failed = True
            return
        if not isinstance(stored, dict) or not isinstance(stored.get("attempts"), list):
            self._storage_failed = True
            return

        attempts: list[SendAttempt] = []
        for item in stored["attempts"]:
            timestamp = item.get("timestamp") if isinstance(item, dict) else None
            digest = item.get("digest") if isinstance(item, dict) else None
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("attempt_id"), str)
                or not isinstance(timestamp, (float, int))
                or isinstance(timestamp, bool)
                or not math.isfinite(timestamp)
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or item.get("phase") not in {"reserved", "accepted", "rejected", "unknown"}
                or (
                    item.get("message_uuid") is not None
                    and not self._is_valid_message_uuid(item.get("message_uuid"))
                )
            ):
                self._storage_failed = True
                self._attempts = []
                return
            attempts.append(cast(SendAttempt, item))
        self._attempts = attempts

    @staticmethod
    def _is_valid_message_uuid(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            return str(UUID(value)) == value
        except ValueError:
            return False

    def _prune_attempts(self, now: float) -> None:
        """Keep rolling guard history and the latest receipt handle."""
        hour_ago = now - 3600
        latest_uuid_attempt = next(
            (
                item for item in reversed(self._attempts)
                if item.get("message_uuid") is not None
            ),
            None,
        )
        self._attempts = [
            item for item in self._attempts
            if item["phase"] in {"reserved", "unknown"}
            or item["timestamp"] > hour_ago
            or item is latest_uuid_attempt
        ]

    @staticmethod
    def _has_store_or_corrupt_backup(path: str) -> bool:
        """Detect a file that Core may quarantine while returning None from async_load."""
        try:
            os.stat(path)
            return True
        except FileNotFoundError:
            pass
        directory, filename = os.path.split(path)
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            return False
        return any(name.startswith(f"{filename}.corrupt.") for name in names)

    async def async_reserve(self, message: str) -> str:
        """Durably reserve before POST; unresolved attempts stay blocked across restarts."""
        async with self._lock:
            if self._storage_failed:
                raise IDigitalError("storage_error")

            now = time.time()
            hour_ago = now - 3600
            duplicate_cutoff = now - 300
            self._prune_attempts(now)
            digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
            if any(
                item["digest"] == digest
                and item["phase"] in {"reserved", "unknown"}
                for item in self._attempts
            ):
                raise IDigitalError("unknown_outcome")
            if any(
                item["digest"] == digest
                and item["timestamp"] > duplicate_cutoff
                for item in self._attempts
            ):
                raise IDigitalError("duplicate_suppressed")

            recent = [item for item in self._attempts if item["timestamp"] > hour_ago]
            unresolved = [
                item for item in self._attempts
                if item["phase"] in {"reserved", "unknown"}
            ]
            if len(unresolved) >= MAX_UNRESOLVED_ATTEMPTS:
                raise IDigitalError("guard_capacity")
            if recent and now - max(item["timestamp"] for item in recent) < 60:
                raise IDigitalError("rate_limited")
            if len(recent) >= 10:
                raise IDigitalError("rate_limited")

            attempt_id = str(uuid4())
            self._attempts.append({
                "attempt_id": attempt_id,
                "timestamp": now,
                "digest": digest,
                "phase": "reserved",
                "message_uuid": None,
            })
            try:
                await self._async_save_and_verify()
            except Exception:
                self._storage_failed = True
                raise IDigitalError("storage_error") from None
            return attempt_id

    async def _async_save_and_verify(self) -> None:
        """Verify a snapshot without releasing the guard during a cancelled write."""
        expected: GuardState = {
            "attempts": [dict(attempt) for attempt in self._attempts],
        }
        persistence = asyncio.create_task(self._async_persist_and_verify(expected))
        try:
            await asyncio.shield(persistence)
        except asyncio.CancelledError:
            # Store.async_save may be cancelled while its executor thread is still
            # atomically replacing the file. Keep the caller's guard lock held until
            # the Store coroutine/readback finish, then permanently fail closed.
            self._storage_failed = True
            while not persistence.done():
                try:
                    await asyncio.shield(persistence)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if persistence.done() and not persistence.cancelled():
                # Retrieve an exception if the cancelled caller stopped awaiting it.
                persistence.exception()
            raise

    async def _async_persist_and_verify(self, expected: GuardState) -> None:
        """Write a snapshot and compare it through a separate Store reader."""
        await self._store.async_save(expected)
        try:
            persisted = await self._readback_store.async_load()
        except Exception:
            raise IDigitalError("storage_error") from None
        if persisted != expected:
            raise IDigitalError("storage_error")

    async def async_finish(
        self,
        attempt_id: str,
        phase: SendPhase,
        *,
        message_uuid: str | None = None,
    ) -> None:
        """Persist the result; failed persistence leaves the attempt fail-closed."""
        async with self._lock:
            attempt = next(
                (item for item in self._attempts if item["attempt_id"] == attempt_id),
                None,
            )
            if attempt is None:
                self._storage_failed = True
                raise IDigitalError("storage_error")
            if message_uuid is not None:
                attempt["message_uuid"] = message_uuid
            attempt["phase"] = phase
            # Keep the newest UUID-bearing receipt alongside the rolling rate-limit
            # window so a restart can restore the explicit status-query handle.
            self._prune_attempts(time.time())
            try:
                await self._async_save_and_verify()
            except Exception:
                self._storage_failed = True
                attempt["phase"] = "unknown"
                raise IDigitalError("unknown_outcome") from None


class IDigitalSmsNotifyEntity(NotifyEntity):
    """Send to the recipient configured in the integration flow."""

    _attr_has_entity_name = True
    _attr_translation_key = "sms"

    def __init__(
        self,
        client: object,
        entry_id: str,
        send_guard: PersistentSendGuard,
    ) -> None:
        self._client = client
        self._attr_unique_id = f"{entry_id}_notify"
        self._message_uuid = send_guard.latest_message_uuid
        self._send_guard = send_guard

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Expose only the last provider UUID, in memory, for explicit status checks."""
        if self._message_uuid is None:
            return None
        return {ATTR_MESSAGE_UUID: self._message_uuid}

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Send once; a timeout is ambiguous and is never retried."""
        try:
            validate_message(message)
            attempt_id = await self._send_guard.async_reserve(message)
        except IDigitalError as err:
            _LOGGER.warning("I-Digital SMS send failed: %s", err.code)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=err.code,
            ) from None
        self._message_uuid = None
        self.async_write_ha_state()
        try:
            message_uuid = await self._client.send_message(message)
        except IDigitalError as err:
            phase: SendPhase = (
                "rejected"
                if err.code in {"authentication_error", "provider_rejected"}
                else "unknown"
            )
            try:
                await self._send_guard.async_finish(attempt_id, phase)
            except IDigitalError as persistence_error:
                err = persistence_error
            _LOGGER.warning("I-Digital SMS send failed: %s", err.code)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=err.code,
            ) from None
        # Keep a known provider handle available in this runtime even if the
        # subsequent durable receipt write fails. The guard still blocks sends.
        self._message_uuid = message_uuid
        self.async_write_ha_state()
        try:
            await self._send_guard.async_finish(
                attempt_id,
                "accepted",
                message_uuid=message_uuid,
            )
        except IDigitalError as err:
            _LOGGER.warning("I-Digital SMS result persistence failed: %s", err.code)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=err.code,
            ) from None
        self.async_write_ha_state()
