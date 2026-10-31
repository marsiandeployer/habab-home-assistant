"""Managed credentials survive native saves; no network or live HA is needed."""

import asyncio
import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home import config_flow, websocket
from homeassistant.components.habab_home.beta import BetaError
from homeassistant.components.habab_home.providers import PROVIDERS, ProviderError
from homeassistant.exceptions import Unauthorized


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entries = []
        self.session = object()
        self.agent = SimpleNamespace(select_provider=Mock())
        self.hass = SimpleNamespace(
            data={"habab_home": {"settings_lock": asyncio.Lock(), "setup_agent": self.agent}},
            config_entries=SimpleNamespace(
                async_entries=Mock(side_effect=lambda domain: list(self.entries)),
                async_update_entry=Mock(side_effect=self.update),
                async_reload=AsyncMock(return_value=True),
                flow=SimpleNamespace(async_init=AsyncMock(side_effect=self.run_flow), async_abort=Mock()),
            ),
        )
        self.enroll = AsyncMock(return_value="fixture-new-managed-key")
        self.installation = AsyncMock(return_value="fixture-installation")
        self.provider = Mock()
        self.provider.return_value.check = AsyncMock()
        for name, value in (("_enroll", self.enroll), ("_installation_id", self.installation),
                            ("ProviderClient", self.provider),
                            ("async_get_clientsession", Mock(return_value=self.session))):
            patcher = patch.object(config_flow, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def entry(self, *, key="fixture-legacy-managed-key", provider="habab"):
        entry = SimpleNamespace(
            unique_id=provider, entry_id="entry-" + provider, domain="habab_home",
            data={"provider": provider, "api_key": key, "model": PROVIDERS[provider]["model"]},
            options={"assistant_selected": True},
        )
        self.entries.append(entry)
        return entry

    def update(self, entry, **changes):
        for key, value in changes.items():
            setattr(entry, key, value)

    def flow(self, context=None):
        flow = config_flow.HababHomeConfigFlow()
        flow.hass = self.hass
        flow.context = context or {}
        flow.async_set_unique_id = AsyncMock()
        flow._async_current_entries = Mock(side_effect=lambda: list(self.entries))
        flow.async_create_entry = Mock(side_effect=lambda title, data: {"type": "create_entry", "data": data})
        flow.async_abort = Mock(side_effect=lambda reason: {"type": "abort", "reason": reason})
        flow.async_show_form = Mock(side_effect=lambda **kwargs: {"type": "form", **kwargs})
        return flow

    async def run_flow(self, domain, *, context, data):
        result = await self.flow(context).async_step_user(data)
        if result["type"] == "create_entry":
            saved = self.entry(key=result["data"]["api_key"], provider=result["data"]["provider"])
            saved.data = result["data"]
            result["result"] = saved
        return result

    def input(self, key="fixture-arbitrary-user-key", provider="habab"):
        return {"provider": provider, "api_key": key, "model": PROVIDERS[provider]["model"]}

    async def test_fresh_activation_ignores_user_key_and_enrolls_once_then_reuses(self):
        for key in (None, "", "fixture-arbitrary-user-key"):
            with self.subTest(key=key):
                self.entries.clear()
                self.enroll.reset_mock()
                data = self.input(key)
                first = await self.run_flow("habab_home", context={}, data=data)
                self.assertEqual(first["data"]["api_key"], "fixture-new-managed-key")
                self.assertEqual(data["api_key"], key)
                again = await self.run_flow("habab_home", context={}, data=data)
                self.assertEqual(again, {"type": "abort", "reason": "reconfigured"})
                self.assertEqual(self.entries[0].data["api_key"], "fixture-new-managed-key")
                self.enroll.assert_awaited_once_with(self.session, "fixture-installation")

    async def test_existing_managed_and_legacy_keys_survive_repeated_save_and_reload(self):
        for saved_key in ("fixture-legacy-managed-key", "hb1." + "a" * 32 + "." + "A" * 43):
            self.entries.clear()
            entry = self.entry(key=saved_key)
            for supplied in (None, "", "fixture-other-key"):
                with self.subTest(saved_key=saved_key, supplied=supplied):
                    result = await self.flow().async_step_user(self.input(supplied))
                    self.assertEqual(result["reason"], "reconfigured")
                    self.assertEqual(entry.data["api_key"], saved_key)
                    self.assertEqual(entry.options, {"assistant_selected": True})
                    self.assertEqual(self.provider.call_args.args[1]["api_key"], saved_key)
            self.hass.config_entries.async_reload.assert_awaited_with(entry.entry_id)
        self.enroll.assert_not_awaited()
        self.installation.assert_not_awaited()

    async def test_websocket_save_uses_native_flow_and_retains_key_across_api_reload(self):
        entry = self.entry()
        original = dict(entry.data)
        connection = SimpleNamespace(send_error=Mock(), send_result=Mock())
        for supplied in (original["api_key"], "fixture-other-key"):
            await inspect.unwrap(websocket.save_connection)(self.hass, connection, {
                "id": 9, **self.input(supplied),
            })
            self.assertEqual(entry.data, original)
            self.hass.config_entries.async_reload.assert_awaited_with(entry.entry_id)
            connection.send_error.assert_not_called()
            self.assertTrue(connection.send_result.call_args.args[1]["saved"])
            self.agent.select_provider.assert_called_with("habab")
        self.enroll.assert_not_awaited()

    async def test_missing_saved_key_enrolls_but_failure_never_replaces_entry(self):
        entry = self.entry(key="")
        self.enroll.side_effect = BetaError("habab_beta_unavailable")
        result = await self.flow().async_step_user(self.input())
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        self.assertEqual(entry.data["api_key"], "")
        self.hass.config_entries.async_update_entry.assert_not_called()
        self.hass.config_entries.async_reload.assert_not_awaited()
        self.enroll.side_effect = None
        result = await self.flow().async_step_user(self.input())
        self.assertEqual(result["reason"], "reconfigured")
        self.assertEqual(entry.data["api_key"], "fixture-new-managed-key")

    async def test_fresh_enrollment_failure_does_not_create_or_accept_user_key(self):
        self.enroll.side_effect = BetaError("habab_beta_unavailable")
        flow = self.flow()
        result = await flow.async_step_user(self.input())
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        flow.async_create_entry.assert_not_called()
        self.provider.assert_not_called()

    async def test_client_input_cannot_enable_internal_enrollment_context(self):
        data = {**self.input(), "habab_beta_enrollment": True}
        result = await self.flow().async_step_user(data)
        self.assertEqual(result["data"]["api_key"], "fixture-new-managed-key")
        self.enroll.assert_awaited_once()

    async def test_save_api_keeps_admin_boundary(self):
        for user in (None, SimpleNamespace(is_admin=False)):
            with self.subTest(user=user):
                connection = SimpleNamespace(user=user, send_error=Mock(), send_result=Mock())
                with self.assertRaises(Unauthorized):
                    websocket.save_connection(self.hass, connection, {"id": 1, **self.input()})
        self.hass.config_entries.flow.async_init.assert_not_awaited()

    async def test_invalid_existing_key_is_explicit_without_rotation(self):
        entry = self.entry(key="bad")
        result = await self.flow().async_step_user(self.input())
        self.assertEqual(result["errors"], {"base": "invalid_auth"})
        self.assertEqual(entry.data["api_key"], "bad")
        self.enroll.assert_not_awaited()
        self.provider.assert_not_called()
        self.hass.config_entries.async_update_entry.assert_not_called()

    async def test_provider_errors_and_reload_failure_do_not_trigger_enrollment(self):
        entry = self.entry()
        original = dict(entry.data)
        for code in ("invalid_auth", "quota", "cannot_connect", "provider_not_ready"):
            with self.subTest(code=code):
                self.provider.return_value.check.side_effect = ProviderError(code)
                result = await self.flow().async_step_user(self.input())
                self.assertEqual(result["errors"], {"base": code})
                self.assertEqual(entry.data, original)
        self.hass.config_entries.async_update_entry.assert_not_called()
        self.provider.return_value.check.side_effect = None
        self.hass.config_entries.async_reload.return_value = False
        result = await self.flow().async_step_user(self.input())
        self.assertEqual(result["reason"], "setup_failed")
        self.assertEqual(entry.data, original)
        self.enroll.assert_not_awaited()

    async def test_internal_beta_context_accepts_already_enrolled_key_without_issuing(self):
        data = self.input("fixture-internally-enrolled-key")
        result = await self.flow({"habab_beta_enrollment": True}).async_step_user(data)
        self.assertEqual(result["data"], data)
        self.assertEqual(result["data"]["model"], PROVIDERS["habab"]["model"])
        self.enroll.assert_not_awaited()

    async def test_internal_beta_context_does_not_displace_existing_key_or_direct_provider(self):
        for provider in ("habab", "deepseek"):
            with self.subTest(provider=provider):
                self.entries.clear()
                entry = self.entry(provider=provider)
                original = dict(entry.data)
                result = await self.flow({"habab_beta_enrollment": True}).async_step_user(self.input())
                self.assertEqual(result["reason"], "already_configured")
                self.assertEqual(entry.data, original)
                self.hass.config_entries.async_update_entry.assert_not_called()
                self.hass.config_entries.async_reload.assert_not_awaited()
        self.enroll.assert_not_awaited()

    async def test_direct_providers_keep_their_explicit_keys_and_models(self):
        for provider in ("openai", "gemini", "deepseek"):
            with self.subTest(provider=provider):
                data = self.input(provider=provider)
                first = await self.flow().async_step_user(data)
                self.assertEqual(first["data"], data)
                entry = self.entry(provider=provider)
                changed = {**data, "api_key": "fixture-new-direct-key", "model": "new-model"}
                result = await self.flow().async_step_user(changed)
                self.assertEqual(result["reason"], "reconfigured")
                self.assertEqual(entry.data, changed)
        self.enroll.assert_not_awaited()
        self.installation.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
