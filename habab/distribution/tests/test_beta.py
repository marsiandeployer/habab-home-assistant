"""Isolated beta activation tests; no HA config or remote gateway is mutated."""

import asyncio
import inspect
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

import aiohttp

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import Unauthorized
from homeassistant.components.habab_home import beta, websocket
from homeassistant.components.habab_home import config_flow
from homeassistant.components.habab_home.providers import ProviderError


INSTALLATION_ID = "88888888-8888-4888-8888-888888888888"
CODE = "hb1." + uuid.UUID(INSTALLATION_ID).hex + "." + "A" * 43


class Body:
    def __init__(self, raw):
        self.raw = raw

    async def iter_chunked(self, size):
        # Split payloads to ensure validation waits for the complete body.
        yield self.raw[:17]
        yield self.raw[17:]


class Response:
    def __init__(self, status=200, *, payload=None, raw=None, content_length=None):
        if payload is None:
            payload = {"installation_id": INSTALLATION_ID, "code": CODE}
        self.status = status
        self.content_length = content_length
        self.content = Body(raw if raw is not None else json.dumps(payload).encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class BetaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entries = []
        self.store_data = None
        self.store = SimpleNamespace(async_load=AsyncMock(side_effect=self.load),
                                     async_save=AsyncMock(side_effect=self.save))
        self.session = SimpleNamespace(post=Mock(return_value=Response()))
        self.agent = SimpleNamespace(select_provider=Mock(side_effect=self.select))
        self.flow = SimpleNamespace(async_init=AsyncMock(side_effect=self.create_entry),
                                    async_abort=Mock())
        self.hass = SimpleNamespace(
            data={"habab_home": {"settings_lock": asyncio.Lock(), "setup_agent": self.agent}},
            config_entries=SimpleNamespace(
                async_entries=Mock(side_effect=lambda domain: list(self.entries)),
                async_get_entry=Mock(side_effect=lambda entry_id: next(
                    (entry for entry in self.entries if entry.entry_id == entry_id), None)),
                async_remove=AsyncMock(side_effect=self.remove_entry),
                flow=self.flow,
            ),
        )
        for name, replacement in (("Store", Mock(return_value=self.store)),
                                  ("async_get_clientsession", Mock(return_value=self.session))):
            patcher = patch.object(beta, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(beta.uuid, "uuid4", return_value=uuid.UUID(INSTALLATION_ID))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def load(self):
        return self.store_data

    async def save(self, data):
        self.store_data = dict(data)

    def select(self, provider):
        for entry in self.entries:
            entry.options["assistant_selected"] = entry.data["provider"] == provider

    async def create_entry(self, domain, *, context, data):
        entry = SimpleNamespace(entry_id="beta-entry", data=dict(data), options={},
                                state=ConfigEntryState.LOADED)
        self.entries.append(entry)
        return {"type": "create_entry", "result": entry}

    async def remove_entry(self, entry_id):
        self.entries[:] = [entry for entry in self.entries if entry.entry_id != entry_id]
        return {"require_restart": False}

    async def assert_error(self, expected):
        with self.assertRaises(beta.BetaError) as error:
            await beta.async_activate(self.hass)
        self.assertEqual(error.exception.code, expected)
        self.assertNotIn(CODE, str(error.exception))

    async def test_fresh_activation_uses_fixed_transport_and_native_flow(self):
        result = await beta.async_activate(self.hass)
        self.assertEqual(result, {"status": "ready", "activated": True,
                                 "provider": "habab", "entry_id": "beta-entry", "state": "loaded"})
        self.assertNotIn(CODE, json.dumps(result))
        self.assertEqual(self.store_data, {"installation_id": INSTALLATION_ID})
        beta.Store.assert_called_once_with(self.hass, 1, beta.STORE_KEY, private=True)
        self.session.post.assert_called_once()
        args, kwargs = self.session.post.call_args
        self.assertEqual(args, ("https://habab-voice.wpmix.net/beta/enroll",))
        self.assertEqual(kwargs["json"], {"installation_id": INSTALLATION_ID})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"].total, beta.ENROLL_TIMEOUT)
        self.flow.async_init.assert_awaited_once_with(
            "habab_home", context={"source": "user", "habab_beta_enrollment": True},
            data={"provider": "habab", "api_key": CODE, "model": beta.PROVIDERS["habab"]["model"]})
        self.agent.select_provider.assert_called_once_with("habab")

    async def test_every_existing_provider_is_preserved_even_when_unloaded(self):
        for provider in beta.PROVIDERS:
            with self.subTest(provider=provider):
                entry = SimpleNamespace(entry_id="old", data={"provider": provider, "api_key": "existing"},
                                        options={"assistant_selected": True},
                                        state=ConfigEntryState.SETUP_ERROR)
                self.entries[:] = [entry]
                result = await beta.async_activate(self.hass)
                self.assertEqual(result["status"], "existing")
                self.assertEqual(result["provider"], provider)
                self.assertEqual(result["state"], "setup_error")
                self.assertFalse(result["activated"])
        self.store.async_load.assert_not_awaited()
        self.flow.async_init.assert_not_awaited()
        self.session.post.assert_not_called()
        self.agent.select_provider.assert_not_called()

    async def test_legacy_unselected_provider_is_preserved(self):
        self.entries[:] = [SimpleNamespace(entry_id="old", data={"provider": "openai"},
                                          options={}, state=ConfigEntryState.LOADED)]
        self.assertEqual((await beta.async_activate(self.hass))["status"], "existing")
        self.session.post.assert_not_called()

    async def test_concurrent_calls_issue_one_code_and_one_flow(self):
        original = self.flow.async_init.side_effect

        async def slow_create(*args, **kwargs):
            await asyncio.sleep(0)
            return await original(*args, **kwargs)

        self.flow.async_init.side_effect = slow_create
        results = await asyncio.gather(*(beta.async_activate(self.hass) for _ in range(4)))
        self.assertEqual([result["status"] for result in results], ["ready", "existing", "existing", "existing"])
        self.session.post.assert_called_once()
        self.flow.async_init.assert_awaited_once()
        self.store.async_save.assert_awaited_once()

    async def test_uses_the_shared_settings_lock_and_rechecks_after_waiting(self):
        lock = self.hass.data["habab_home"]["settings_lock"]
        await lock.acquire()
        activation = asyncio.create_task(beta.async_activate(self.hass))
        await asyncio.sleep(0)
        self.session.post.assert_not_called()
        self.entries.append(SimpleNamespace(entry_id="manual", data={"provider": "deepseek"},
                                            options={}, state=ConfigEntryState.LOADED))
        lock.release()
        self.assertEqual((await activation)["status"], "existing")
        self.session.post.assert_not_called()

    async def test_manual_provider_created_during_enrollment_is_preserved(self):
        manual = SimpleNamespace(entry_id="manual-openai", data={"provider": "openai", "api_key": "manual"},
                                 options={"assistant_selected": True}, state=ConfigEntryState.LOADED)

        async def enrolled(*args):
            self.entries.append(manual)
            return CODE

        with patch.object(beta, "_enroll", side_effect=enrolled):
            result = await beta.async_activate(self.hass)
        self.assertEqual(result["status"], "existing")
        self.assertEqual(result["entry_id"], manual.entry_id)
        self.assertEqual(manual.options, {"assistant_selected": True})
        self.assertEqual(manual.data["api_key"], "manual")
        self.flow.async_init.assert_not_awaited()
        self.agent.select_provider.assert_not_called()

    async def test_manual_provider_created_during_native_flow_is_preserved(self):
        for provider, options in (("openai", {"assistant_selected": True}), ("gemini", {})):
            with self.subTest(provider=provider):
                self.entries.clear()
                manual = SimpleNamespace(entry_id="manual-" + provider,
                                         data={"provider": provider, "api_key": "manual"},
                                         options=dict(options), state=ConfigEntryState.LOADED)

                async def concurrent_flow(*args, **kwargs):
                    self.entries.append(manual)
                    await asyncio.sleep(0)
                    return await self.create_entry(*args, **kwargs)

                self.flow.async_init.side_effect = concurrent_flow
                result = await beta.async_activate(self.hass)
                self.assertEqual(result["status"], "existing")
                self.assertEqual(result["entry_id"], manual.entry_id)
                self.assertEqual(self.entries, [manual])
                self.assertEqual(manual.options, options)
                self.assertEqual(manual.data["api_key"], "manual")
                self.assertIs(beta.effective_provider_entry(self.entries), manual)
        self.agent.select_provider.assert_not_called()

    async def test_transport_retry_reuses_persisted_id(self):
        self.session.post.side_effect = [TimeoutError("secret"), Response()]
        await self.assert_error("habab_beta_timeout")
        self.assertEqual(self.store_data, {"installation_id": INSTALLATION_ID})
        await beta.async_activate(self.hass)
        self.store.async_save.assert_awaited_once()
        self.assertEqual([call.kwargs["json"] for call in self.session.post.call_args_list],
                         [{"installation_id": INSTALLATION_ID}] * 2)

    async def test_storage_failure_never_enrolls(self):
        self.store.async_save.side_effect = OSError("private")
        await self.assert_error("habab_beta_storage_failed")
        self.session.post.assert_not_called()

    async def test_invalid_persisted_identity_never_replaces_or_enrolls(self):
        for value in ({}, {"installation_id": "bad"},
                      {"installation_id": "88888888-8888-1888-8888-888888888888"}):
            self.store_data = value
            await self.assert_error("habab_beta_storage_failed")
        self.store.async_save.assert_not_awaited()
        self.session.post.assert_not_called()

    async def test_http_errors_are_safe_and_never_start_config_flow(self):
        for status, expected in ((403, "habab_beta_issuance_disabled"),
                                 (429, "habab_beta_rate_limited"),
                                 (503, "habab_beta_unavailable"),
                                 (302, "habab_beta_unavailable")):
            self.session.post.return_value = Response(status=status, raw=CODE.encode())
            await self.assert_error(expected)
        self.flow.async_init.assert_not_awaited()

    async def test_network_errors_are_safe(self):
        self.session.post.side_effect = aiohttp.ClientError(CODE)
        await self.assert_error("habab_beta_cannot_connect")

    async def test_response_shape_and_size_are_checked_before_using_code(self):
        invalid = [Response(raw=b"not-json"), Response(payload=[]),
                   Response(raw=b"[" * 1010 + b"]" * 1010),
                   Response(payload={"installation_id": INSTALLATION_ID, "code": "bad"}),
                   Response(payload={"installation_id": "99999999-9999-4999-9999-999999999999", "code": CODE}),
                   Response(payload={"installation_id": INSTALLATION_ID,
                                     "code": "hb1." + "b" * 32 + "." + "A" * 43}),
                   Response(raw=b" " * (beta.MAX_RESPONSE_BYTES + 1)),
                   Response(content_length=beta.MAX_RESPONSE_BYTES + 1)]
        for response in invalid:
            self.session.post.return_value = response
            await self.assert_error("habab_beta_invalid_response")
        self.flow.async_init.assert_not_awaited()

    async def test_provider_check_failure_is_returned_and_retry_uses_same_id(self):
        self.flow.async_init.side_effect = None
        self.flow.async_init.return_value = {"type": "form", "flow_id": "rejected",
                                            "errors": {"base": "invalid_auth"}}
        await self.assert_error("invalid_auth")
        self.flow.async_abort.assert_called_once_with("rejected")
        self.assertEqual(self.store_data, {"installation_id": INSTALLATION_ID})
        self.agent.select_provider.assert_not_called()

    async def test_manual_habab_flow_generates_own_code_for_empty_or_arbitrary_key(self):
        for supplied in (None, "", "anything"):
            with self.subTest(supplied=supplied):
                flow = config_flow.HababHomeConfigFlow()
                flow.hass = self.hass
                flow.context = {}
                flow.async_set_unique_id = AsyncMock()
                flow._async_current_entries = Mock(return_value=[])
                flow.async_create_entry = Mock(side_effect=lambda title, data: {"type": "create_entry", "data": data})
                data = {"provider": "habab", "model": beta.PROVIDERS["habab"]["model"]}
                if supplied is not None:
                    data["api_key"] = supplied
                with patch.object(config_flow, "ProviderClient") as provider, patch.object(
                        config_flow, "async_get_clientsession", return_value=self.session):
                    provider.return_value.check = AsyncMock()
                    result = await flow.async_step_user(data)
                self.assertEqual(result["data"]["api_key"], CODE)
                self.assertNotEqual(data.get("api_key"), CODE)
                provider.return_value.check.assert_awaited_once()

    async def test_activation_composes_with_real_native_config_flow_check(self):
        native_flow = config_flow.HababHomeConfigFlow()
        native_flow.hass = self.hass
        native_flow.async_set_unique_id = AsyncMock()
        native_flow._async_current_entries = Mock(return_value=[])
        native_flow.async_create_entry = Mock(side_effect=lambda title, data: {
            "type": "create_entry", "data": data,
        })

        async def run_native_flow(domain, *, context, data):
            native_flow.context = context
            result = await native_flow.async_step_user(data)
            if result["type"] == "create_entry":
                return await self.create_entry(domain, context=context, data=result["data"])
            return {**result, "flow_id": "native-rejected"}

        self.flow.async_init.side_effect = run_native_flow
        with patch.object(config_flow, "ProviderClient") as provider, \
                patch.object(config_flow, "async_get_clientsession", return_value=self.session):
            provider.return_value.check = AsyncMock(side_effect=ProviderError("quota"))
            await self.assert_error("quota")
            self.assertEqual(self.entries, [])
            self.agent.select_provider.assert_not_called()
            provider.return_value.check.side_effect = None
            result = await beta.async_activate(self.hass)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(provider.return_value.check.await_count, 2)
            native_flow.async_set_unique_id.assert_awaited_once_with("habab")

    async def test_native_beta_flow_preserves_concurrent_manual_habab_key(self):
        manual = SimpleNamespace(entry_id="manual-habab", unique_id="habab",
                                 data={"provider": "habab", "api_key": "manual-key", "model": "manual-model"},
                                 options={"assistant_selected": True}, state=ConfigEntryState.LOADED)
        native_flow = config_flow.HababHomeConfigFlow()
        native_flow.hass = self.hass
        native_flow.async_set_unique_id = AsyncMock()
        native_flow._async_current_entries = Mock(side_effect=lambda: list(self.entries))
        native_flow.async_create_entry = Mock()
        self.hass.config_entries.async_update_entry = Mock()
        self.hass.config_entries.async_reload = AsyncMock()

        async def provider_check():
            await asyncio.sleep(0)
            self.entries.append(manual)

        async def run_native_flow(domain, *, context, data):
            native_flow.context = context
            return await native_flow.async_step_user(data)

        self.flow.async_init.side_effect = run_native_flow
        with patch.object(config_flow, "ProviderClient") as provider, \
                patch.object(config_flow, "async_get_clientsession", return_value=self.session):
            provider.return_value.check = AsyncMock(side_effect=provider_check)
            result = await beta.async_activate(self.hass)
        self.assertEqual(result["status"], "existing")
        self.assertEqual(result["entry_id"], manual.entry_id)
        self.assertEqual(manual.data["api_key"], "manual-key")
        self.assertEqual(manual.data["model"], "manual-model")
        self.assertEqual(manual.options, {"assistant_selected": True})
        self.assertEqual(self.entries, [manual])
        self.agent.select_provider.assert_not_called()
        native_flow.async_create_entry.assert_not_called()
        self.hass.config_entries.async_update_entry.assert_not_called()
        self.hass.config_entries.async_reload.assert_not_awaited()
        self.hass.config_entries.async_remove.assert_not_awaited()

    async def test_unknown_flow_errors_and_exceptions_never_leak(self):
        self.flow.async_init.side_effect = None
        for result in ({"type": "form", "flow_id": "bad", "errors": {"base": CODE}},
                       {"type": "abort", "reason": "reconfigured"},
                       {"type": "create_entry", "result": None}):
            self.flow.async_init.return_value = result
            await self.assert_error("habab_beta_setup_failed")
        self.flow.async_init.side_effect = RuntimeError(CODE)
        await self.assert_error("habab_beta_setup_failed")

    async def test_not_loaded_or_missing_or_changed_entry_never_reports_ready(self):
        for mode in ("not_loaded", "missing", "changed"):
            self.entries.clear()

            async def failed_create(*args, **kwargs):
                result = await self.create_entry(*args, **kwargs)
                if mode == "not_loaded":
                    result["result"].state = ConfigEntryState.SETUP_ERROR
                elif mode == "changed":
                    result["result"].data["api_key"] = "other"
                else:
                    self.entries.clear()
                return result

            self.flow.async_init.side_effect = failed_create
            await self.assert_error("habab_beta_setup_failed")
        self.agent.select_provider.assert_not_called()

    async def test_failed_selection_never_reports_ready(self):
        self.agent.select_provider.side_effect = RuntimeError(CODE)
        await self.assert_error("habab_beta_setup_failed")

    async def test_websocket_boundary_requires_admin_and_returns_secret_free_result(self):
        connection = SimpleNamespace(user=SimpleNamespace(is_admin=False),
                                     send_error=Mock(), send_result=Mock())
        for user in (None, SimpleNamespace(is_admin=False)):
            connection.user = user
            with self.assertRaises(Unauthorized):
                websocket.activate_beta(self.hass, connection, {"id": 9})
        await inspect.unwrap(websocket.activate_beta)(self.hass, connection, {"id": 9})
        result = connection.send_result.call_args.args[1]
        self.assertEqual(result["status"], "ready")
        self.assertNotIn(CODE, json.dumps(result))
        connection.send_result.reset_mock()
        self.entries.clear()
        self.session.post.return_value = Response(status=403)
        await inspect.unwrap(websocket.activate_beta)(self.hass, connection, {"id": 10})
        connection.send_error.assert_called_once_with(10, "habab_beta_issuance_disabled",
                                                      "habab_beta_issuance_disabled")
        connection.send_result.assert_not_called()


if __name__ == "__main__":
    unittest.main()
