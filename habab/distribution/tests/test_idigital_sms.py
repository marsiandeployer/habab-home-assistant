"""Offline protocol tests for the single-recipient I-Digital Direct client."""

import asyncio
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch


_SRC = Path(__file__).parents[1] / "src" / "idigital_sms"
_PACKAGE = "_idigital_sms_protocol_tests"
_package = types.ModuleType(_PACKAGE)
_package.__path__ = [str(_SRC)]
sys.modules.setdefault(_PACKAGE, _package)
_spec = importlib.util.spec_from_file_location(
    f"{_PACKAGE}.api", _SRC / "api.py"
)
_api = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _api
_spec.loader.exec_module(_api)


class _FakeNotifyEntity:
    def async_write_ha_state(self):
        pass


class _FakeHomeAssistantError(Exception):
    def __init__(
        self,
        *args,
        translation_key=None,
        translation_domain=None,
        **_kwargs,
    ):
        super().__init__(*args or (translation_key,))
        self.translation_key = translation_key
        self.translation_domain = translation_domain


class _FakeStore:
    def __class_getitem__(cls, _item):
        return cls


def _load_notify_module():
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    components = types.ModuleType("homeassistant.components")
    components.__path__ = []
    notify = types.ModuleType("homeassistant.components.notify")
    notify.NotifyEntity = _FakeNotifyEntity
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = _FakeHomeAssistantError
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = _FakeStore
    with patch.dict(sys.modules, {
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.notify": notify,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.storage": storage,
    }):
        spec = importlib.util.spec_from_file_location(
            f"{_PACKAGE}.notify", _SRC / "notify.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


def _load_integration_module():
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    const = types.ModuleType("homeassistant.const")
    const.Platform = types.SimpleNamespace(NOTIFY="notify")
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.ServiceCall = object
    core.SupportsResponse = types.SimpleNamespace(ONLY="only")
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = _FakeHomeAssistantError
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: hass.session
    voluptuous = types.ModuleType("voluptuous")
    voluptuous.Schema = lambda schema: schema
    voluptuous.Required = lambda key: key
    with patch.dict(sys.modules, {
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.config_entries": config_entries,
        "homeassistant.const": const,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.aiohttp_client": aiohttp_client,
        "voluptuous": voluptuous,
    }):
        spec = importlib.util.spec_from_file_location(
            f"{_PACKAGE}.integration", _SRC / "__init__.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


_notify = _load_notify_module()
PersistentSendGuard = _notify.PersistentSendGuard
IDigitalSmsNotifyEntity = _notify.IDigitalSmsNotifyEntity

DirectClient = _api.DirectClient
IDigitalError = _api.IDigitalError
validate_api_key = _api.validate_api_key
validate_recipient = _api.validate_recipient
SendGuard = _api.SendGuard


class FakeContent:
    def __init__(self, body, fragment_size=None):
        self._body = json.dumps(body).encode()
        self._fragment_size = fragment_size

    async def iter_chunked(self, requested_size):
        size = min(self._fragment_size or requested_size, requested_size)
        for offset in range(0, len(self._body), size):
            yield self._body[offset:offset + size]


class FakeResponse:
    def __init__(self, status, body, *, fragment_size=None):
        self.status = status
        self.content = FakeContent(body, fragment_size)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class FakeSession:
    def __init__(self, response=None, *, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response


class FakeHass:
    async def async_add_executor_job(self, function, *args):
        return function(*args)


class MemoryStore:
    def __init__(self, state, *, fail_saves=()):
        self.state = state
        self.fail_saves = set(fail_saves)
        self.save_count = 0
        self.path = "/nonexistent/test/store"
        self.hass = FakeHass()

    async def async_load(self):
        return copy.deepcopy(self.state)

    async def async_save(self, state):
        self.save_count += 1
        if self.save_count in self.fail_saves:
            raise OSError("synthetic store failure")
        self.state.clear()
        self.state.update(copy.deepcopy(state))


class FakeServiceRegistry:
    def __init__(self):
        self.handlers = {}
        self.options = {}

    def has_service(self, domain, service):
        return (domain, service) in self.handlers

    def async_register(self, domain, service, handler, **_kwargs):
        service_key = (domain, service)
        self.handlers[service_key] = handler
        self.options[service_key] = _kwargs


class FakeConfigEntries:
    def __init__(self):
        self.forwarded = []

    async def async_forward_entry_setups(self, entry, platforms):
        self.forwarded.append((entry, platforms))


def make_client(session):
    # Test-only credentials and RFC 5737 documentation address; never transmitted.
    api_key = base64.b64encode(b"123:placeholder").decode()
    return DirectClient(
        session,
        api_key=api_key,
        sender="approved-sender",
        recipient="+19995550123",
    )


class IDigitalSmsProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_entry_setup_is_local_and_status_service_uses_explicit_lookup(self):
        integration = _load_integration_module()
        session = FakeSession(FakeResponse(200, {"status": "delivered"}))
        hass = types.SimpleNamespace(
            data={},
            session=session,
            services=FakeServiceRegistry(),
            config_entries=FakeConfigEntries(),
        )
        entry = types.SimpleNamespace(
            entry_id="test-entry",
            data={
                "api_key": base64.b64encode(b"123:placeholder").decode(),
                "sender": "approved-sender",
                "recipient": "+19995550123",
            },
        )

        self.assertTrue(await integration.async_setup_entry(hass, entry))
        self.assertEqual(session.calls, [])
        self.assertEqual(len(hass.config_entries.forwarded), 1)
        self.assertEqual(
            hass.services.options[("idigital_sms", "get_status")]["supports_response"],
            "only",
        )
        self.assertIn(
            "message_uuid",
            hass.services.options[("idigital_sms", "get_status")]["schema"],
        )

        status_service = hass.services.handlers[("idigital_sms", "get_status")]
        result = await status_service(types.SimpleNamespace(data={
            "message_uuid": "063474ec-a34f-4558-90c5-984395000004",
        }))

        self.assertEqual(result, {
            "status": "delivered",
            "provider_status": "delivered",
        })
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][0], "GET")

    async def test_status_service_errors_are_translated_before_network(self):
        integration = _load_integration_module()
        session = FakeSession(FakeResponse(503, {"error": "unavailable"}))
        hass = types.SimpleNamespace(
            data={},
            session=session,
            services=FakeServiceRegistry(),
            config_entries=FakeConfigEntries(),
        )
        entry = types.SimpleNamespace(
            entry_id="test-entry",
            data={
                "api_key": base64.b64encode(b"123:placeholder").decode(),
                "sender": "approved-sender",
                "recipient": "+19995550123",
            },
        )
        await integration.async_setup_entry(hass, entry)
        status_service = hass.services.handlers[("idigital_sms", "get_status")]

        with self.assertRaises(_FakeHomeAssistantError) as invalid:
            await status_service(types.SimpleNamespace(data={"message_uuid": "bad"}))
        self.assertEqual(invalid.exception.translation_domain, "idigital_sms")
        self.assertEqual(invalid.exception.translation_key, "invalid_message_uuid")
        self.assertEqual(session.calls, [])

        with self.assertRaises(_FakeHomeAssistantError) as unavailable:
            await status_service(types.SimpleNamespace(data={
                "message_uuid": "063474ec-a34f-4558-90c5-984395000004",
            }))
        self.assertEqual(unavailable.exception.translation_domain, "idigital_sms")
        self.assertEqual(unavailable.exception.translation_key, "status_unavailable")
        self.assertEqual(len(session.calls), 1)

        for path in (
            _SRC / "strings.json",
            _SRC / "translations" / "en.json",
            _SRC / "translations" / "ru.json",
        ):
            translations = json.loads(path.read_text())
            self.assertIn(
                "invalid_message_uuid",
                translations["exceptions"],
            )
            self.assertIn("status_unavailable", translations["exceptions"])

    async def test_fragmented_valid_response_is_read_through_eof(self):
        message_uuid = "063474ec-a34f-4558-90c5-984395000004"
        session = FakeSession(FakeResponse(200, {
            "errors": False,
            "items": [{"messageUuid": message_uuid}],
        }, fragment_size=7))
        client = make_client(session)

        result = await client.send_message("Test body")

        self.assertEqual(result, message_uuid)
        self.assertEqual(len(session.calls), 1)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/message"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Basic MTIzOnBsYWNlaG9sZGVy")
        self.assertEqual(kwargs["json"], [{
            "channelType": "SMS",
            "senderName": "approved-sender",
            "destination": "19995550123",
            "content": "Test body",
        }])
        self.assertNotIn("callbackUrl", kwargs["json"][0])

    async def test_invalid_credentials_and_recipient_fail_before_network(self):
        session = FakeSession(FakeResponse(200, {"errors": False, "items": []}))

        with self.assertRaises(IDigitalError):
            validate_api_key("not-a-direct-key")
        with self.assertRaises(IDigitalError):
            validate_recipient("+1 999 555 0123")
        with self.assertRaises(IDigitalError):
            DirectClient(session, api_key="bad", sender="sender", recipient="+19995550123")

        self.assertEqual(session.calls, [])

    async def test_rejected_response_is_not_retried(self):
        session = FakeSession(FakeResponse(200, {
            "errors": True,
            "items": [],
        }))
        client = make_client(session)

        with self.assertRaises(IDigitalError) as raised:
            await client.send_message("Test body")

        self.assertEqual(raised.exception.code, "provider_rejected")
        self.assertEqual(len(session.calls), 1)

    async def test_ambiguous_network_failure_is_not_retried(self):
        session = FakeSession(error=OSError("offline fixture"))
        client = make_client(session)

        with self.assertRaises(IDigitalError) as raised:
            await client.send_message("Test body")

        self.assertEqual(raised.exception.code, "unknown_outcome")
        self.assertEqual(len(session.calls), 1)

    async def test_status_is_explicit_and_404_does_not_claim_delivery(self):
        message_uuid = "063474ec-a34f-4558-90c5-984395000004"
        session = FakeSession(FakeResponse(404, {"message": "not visible yet"}))
        client = make_client(session)

        result = await client.get_status(message_uuid)

        self.assertEqual(result, {"status": "unknown", "provider_status": "not_found"})
        self.assertEqual(len(session.calls), 1)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith(f"/message/{message_uuid}"))
        self.assertIsNone(kwargs["json"])

    async def test_delivered_requires_an_explicit_provider_status_response(self):
        message_uuid = "063474ec-a34f-4558-90c5-984395000004"
        session = FakeSession(FakeResponse(200, {"status": "delivered"}))
        client = make_client(session)

        result = await client.get_status(message_uuid)

        self.assertEqual(result, {
            "status": "delivered",
            "provider_status": "delivered",
        })
        self.assertEqual(len(session.calls), 1)

    async def test_only_documented_direct_statuses_are_classified(self):
        self.assertEqual(_api._message_status("sent"), "accepted")
        self.assertEqual(_api._message_status("delivered"), "delivered")
        self.assertEqual(_api._message_status("undelivered"), "error")
        for status in ("queued", "sending", "expired", "unsent", "stopped", "future"):
            self.assertEqual(_api._message_status(status), "unknown")

        session = FakeSession(FakeResponse(200, {"status": "queued"}))
        result = await make_client(session).get_status(
            "063474ec-a34f-4558-90c5-984395000004"
        )
        self.assertEqual(result, {"status": "unknown", "provider_status": "unknown"})

    async def test_send_guard_suppresses_duplicate_and_enforces_minute_limit(self):
        now = [1000.0]
        guard = SendGuard(clock=lambda: now[0])
        guard.reserve("same message")
        with self.assertRaises(IDigitalError) as duplicate:
            guard.reserve("same message")
        self.assertEqual(duplicate.exception.code, "duplicate_suppressed")
        now[0] += 59.999
        with self.assertRaises(IDigitalError) as limited:
            guard.reserve("different message")
        self.assertEqual(limited.exception.code, "rate_limited")
        now[0] += 0.002
        guard.reserve("different message")

    async def test_send_guard_enforces_ten_attempts_per_rolling_hour(self):
        now = [1000.0]
        guard = SendGuard(clock=lambda: now[0])
        for index in range(10):
            guard.reserve(f"message {index}")
            now[0] += 60
        with self.assertRaises(IDigitalError) as limited:
            guard.reserve("message eleven")
        self.assertEqual(limited.exception.code, "rate_limited")
        now[0] += 3600
        guard.reserve("message after window")

    async def test_outer_timeout_is_bounded_and_not_retried(self):
        class HangingSession:
            def __init__(self):
                self.calls = 0

            def request(self, *_args, **_kwargs):
                self.calls += 1
                return self

            async def __aenter__(self):
                await asyncio.sleep(1)

            async def __aexit__(self, *_args):
                return None

        session = HangingSession()
        client = make_client(session)
        old_timeout = _api.REQUEST_TIMEOUT
        _api.REQUEST_TIMEOUT = 0.001
        try:
            with self.assertRaises(IDigitalError) as raised:
                await client.send_message("Test body")
        finally:
            _api.REQUEST_TIMEOUT = old_timeout

        self.assertEqual(raised.exception.code, "unknown_outcome")
        self.assertEqual(session.calls, 1)

    async def test_acknowledged_uuid_is_saved_and_restored_after_restart(self):
        initial = {"attempts": []}
        writer = MemoryStore(initial)
        reader = MemoryStore(initial)
        guard = PersistentSendGuard(writer, reader)
        await guard.async_load()

        attempt_id = await guard.async_reserve("Test body")
        await guard.async_finish(
            attempt_id,
            "accepted",
            message_uuid="063474ec-a34f-4558-90c5-984395000004",
        )

        self.assertEqual(writer.state["attempts"][0]["phase"], "accepted")
        self.assertEqual(
            writer.state["attempts"][0]["message_uuid"],
            "063474ec-a34f-4558-90c5-984395000004",
        )
        restarted_guard = PersistentSendGuard(writer, reader)
        await restarted_guard.async_load()
        entity = IDigitalSmsNotifyEntity(object(), "entry", restarted_guard)
        self.assertEqual(
            entity.extra_state_attributes,
            {"message_uuid": "063474ec-a34f-4558-90c5-984395000004"},
        )

    async def test_old_guard_records_without_uuid_still_load(self):
        old_record = {
            "attempt_id": "old-attempt",
            "timestamp": time.time(),
            "digest": hashlib.sha256(b"old message").hexdigest(),
            "phase": "accepted",
        }
        store = MemoryStore({"attempts": [old_record]})
        guard = PersistentSendGuard(store, store)

        await guard.async_load()

        self.assertIsNone(guard.latest_message_uuid)
        self.assertEqual(guard._attempts, [old_record])

    async def test_uuid_stays_available_in_memory_if_receipt_write_fails(self):
        class SendingClient:
            async def send_message(self, _message):
                return "063474ec-a34f-4558-90c5-984395000004"

        initial = {"attempts": []}
        writer = MemoryStore(initial, fail_saves={2})
        reader = MemoryStore(initial)
        guard = PersistentSendGuard(writer, reader)
        await guard.async_load()
        entity = IDigitalSmsNotifyEntity(SendingClient(), "entry", guard)

        with self.assertRaises(_FakeHomeAssistantError):
            await entity.async_send_message("Test body")

        self.assertEqual(
            entity.extra_state_attributes,
            {"message_uuid": "063474ec-a34f-4558-90c5-984395000004"},
        )
        self.assertEqual(writer.state["attempts"][0]["phase"], "reserved")
        with self.assertRaises(IDigitalError) as raised:
            await guard.async_reserve("Another message")
        self.assertEqual(raised.exception.code, "storage_error")

    async def test_ambiguous_send_stays_unknown_after_restart_without_retry(self):
        class AmbiguousClient:
            def __init__(self):
                self.calls = 0

            async def send_message(self, _message):
                self.calls += 1
                raise IDigitalError("unknown_outcome")

        initial = {"attempts": []}
        writer = MemoryStore(initial)
        reader = MemoryStore(initial)
        guard = PersistentSendGuard(writer, reader)
        await guard.async_load()
        client = AmbiguousClient()
        entity = IDigitalSmsNotifyEntity(client, "entry", guard)

        with self.assertRaises(_FakeHomeAssistantError):
            await entity.async_send_message("Test body")

        self.assertEqual(writer.state["attempts"][0]["phase"], "unknown")
        restarted_guard = PersistentSendGuard(writer, reader)
        await restarted_guard.async_load()
        with self.assertRaises(IDigitalError) as raised:
            await restarted_guard.async_reserve("Test body")
        self.assertEqual(raised.exception.code, "unknown_outcome")
        self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
