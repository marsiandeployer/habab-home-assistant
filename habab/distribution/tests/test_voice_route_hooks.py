"""The merged native route prerequisite uses the current authenticated action bus."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from homeassistant.core import SupportsResponse
from homeassistant.components.habab_home.setup_actions import execute
from homeassistant.components.habab_home.voice_route import async_setup_voice_route


class VoiceRouteHooksTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = SimpleNamespace(is_active=True, is_admin=True)
        self.hass = SimpleNamespace(
            auth=SimpleNamespace(async_get_user=AsyncMock(return_value=self.user)),
            data={"habab_home": {}},
            services=SimpleNamespace(async_call=AsyncMock(return_value={"status": "queued"}),
                                     async_register=Mock()),
            http=SimpleNamespace(register_view=Mock()),
        )

    async def test_action_hooks_return_service_result_and_carry_owner_context(self):
        for body, service, data in (
            ({"action": "select_voice_route", "target": "phone"}, "select_voice_route", {"target": "phone"}),
            ({"action": "set_voice_wake", "enabled": False}, "set_voice_wake", {"enabled": False}),
        ):
            with self.subTest(service=service):
                result = await execute(self.hass, None, "alice", body)
                self.assertEqual(result, {"status": "queued"})
                call = self.hass.services.async_call.call_args
                self.assertEqual(call.args, ("habab_home", service, data))
                self.assertEqual(call.kwargs["context"].user_id, "alice")
                self.assertTrue(call.kwargs["return_response"])
                self.assertTrue(call.kwargs["blocking"])

    async def test_route_status_and_actions_require_current_active_administrator(self):
        mailbox = SimpleNamespace(status=Mock(return_value={"status": "unavailable"}))
        self.hass.data["habab_home"]["voice_route"] = mailbox
        result = await execute(self.hass, None, "alice", {"action": "voice_route_status"})
        self.assertEqual(result, {"status": "unavailable"})
        mailbox.status.assert_called_once_with("alice")
        self.user.is_admin = False
        for body in ({"action": "voice_route_status"},
                     {"action": "select_voice_route", "target": "phone"},
                     {"action": "set_voice_wake", "enabled": False}):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "not_allowed"):
                await execute(self.hass, None, "alice", body)
        self.hass.services.async_call.assert_not_awaited()

    async def test_extra_action_parameters_never_reach_native_service(self):
        for body in ({"action": "voice_route_status", "owner": "bob"},
                     {"action": "select_voice_route", "target": "phone", "enabled": True},
                     {"action": "set_voice_wake", "enabled": True, "target": "phone"}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                await execute(self.hass, None, "alice", body)
        self.hass.services.async_call.assert_not_awaited()

    async def test_real_ha_service_hooks_register_response_only_and_queue_active_voice(self):
        await async_setup_voice_route(self.hass)
        registered = {call.args[1]: call for call in self.hass.services.async_register.call_args_list}
        self.assertEqual(set(registered), {"select_voice_route", "set_voice_wake"})
        self.assertIs(registered["select_voice_route"].kwargs["supports_response"], SupportsResponse.ONLY)
        self.assertIs(registered["set_voice_wake"].kwargs["supports_response"], SupportsResponse.ONLY)
        self.hass.http.register_view.assert_called_once()
        mailbox = self.hass.data["habab_home"]["voice_route"]
        mailbox.poll("alice", {"safe_to_switch": False, "active_voice": True,
            "wake_enabled": True, "headset_available": True, "phase": "thinking",
            "selected_target": "phone", "actual_route": "phone", "input_type": 15})
        handler = registered["select_voice_route"].args[2]
        result = await handler(SimpleNamespace(context=SimpleNamespace(user_id="alice"),
                                              data={"target": "bluetooth"}))
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["requested_target"], "bluetooth")
        self.assertEqual(mailbox.status("alice")["actual_route"], "phone")
        self.assertTrue(mailbox.status("alice")["pending"])


if __name__ == "__main__":
    unittest.main()
