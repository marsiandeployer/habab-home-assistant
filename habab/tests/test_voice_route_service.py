import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

INTEGRATION = Path(__file__).parents[1] / "distribution/src/habab_home"


class SupportsResponse:
    ONLY = "only"


class FakeUser:
    is_active = True
    is_admin = True


class FakeServices:
    def __init__(self):
        self.handlers = {}
        self.options = {}

    def async_register(self, domain, service, handler, **options):
        self.handlers[(domain, service)] = handler
        self.options[(domain, service)] = options


class FakeHass:
    def __init__(self):
        self.data = {"habab_home": {}}
        self.services = FakeServices()
        self.http = SimpleNamespace(register_view=lambda view: None)
        self.auth = SimpleNamespace(async_get_user=self._get_user)

    async def _get_user(self, owner):
        return FakeUser() if owner == "owner" else None


class VoiceRouteServiceTests(unittest.TestCase):
    def setUp(self):
        self.package = ModuleType("qa_habab_home")
        self.package.__path__ = [str(INTEGRATION)]
        self.stubs = {
            "homeassistant": ModuleType("homeassistant"),
            "homeassistant.components": ModuleType("homeassistant.components"),
            "homeassistant.components.http": ModuleType("homeassistant.components.http"),
            "homeassistant.core": ModuleType("homeassistant.core"),
            "voluptuous": ModuleType("voluptuous"),
        }
        self.stubs["homeassistant.components.http"].HomeAssistantView = type("HomeAssistantView", (), {})
        core = self.stubs["homeassistant.core"]
        core.HomeAssistant = object
        core.HomeAssistantError = RuntimeError
        core.SupportsResponse = SupportsResponse
        vol = self.stubs["voluptuous"]
        vol.Required = lambda key: key
        vol.In = lambda values: tuple(values)
        vol.Any = lambda *values: values
        vol.Schema = lambda schema: schema
        self.module_context = patch.dict(sys.modules, self.stubs)
        self.module_context.__enter__()
        sys.modules["qa_habab_home"] = self.package
        self.module = importlib.import_module("qa_habab_home.voice_route")

    def tearDown(self):
        for name in tuple(sys.modules):
            if name == "qa_habab_home" or name.startswith("qa_habab_home."):
                del sys.modules[name]
        self.module_context.__exit__(None, None, None)

    def test_registered_services_are_response_only_and_return_queued_for_active_voice(self):
        async def run():
            hass = FakeHass()
            await self.module.async_setup_voice_route(hass)
            self.assertEqual(hass.services.options[("habab_home", "select_voice_route")]["supports_response"],
                             SupportsResponse.ONLY)
            self.assertEqual(hass.services.options[("habab_home", "set_voice_wake")]["supports_response"],
                             SupportsResponse.ONLY)
            mailbox = hass.data["habab_home"]["voice_route"]
            mailbox.poll("owner", {"safe_to_switch": False, "active_voice": True,
                "wake_enabled": True, "headset_available": True,
                "phase": "thinking", "selected_target": "phone",
                "actual_route": "phone", "input_type": 15})
            call = SimpleNamespace(context=SimpleNamespace(user_id="owner"),
                                   data={"target": "bluetooth"})
            result = await hass.services.handlers[("habab_home", "select_voice_route")](call)
            self.assertEqual(result["status"], "queued")
            self.assertIn("после сигнала окончания", result["message"])

            mailbox.poll("owner", {"safe_to_switch": True, "active_voice": False,
                "wake_enabled": True, "headset_available": True, "phase": "wake",
                "selected_target": "bluetooth", "actual_route": "bluetooth", "input_type": 7}, {
                "id": result["command_id"], "result": "applied", "actual_route": "bluetooth",
                "input_type": 7, "wake_enabled": True, "error": "",
            })
            mailbox.poll("owner", {"safe_to_switch": True, "active_voice": False,
                "wake_enabled": True, "headset_available": False, "phase": "wake",
                "selected_target": "phone", "actual_route": "phone", "input_type": 15})
            result = await hass.services.handlers[("habab_home", "select_voice_route")](call)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"], "headset_unavailable")

        asyncio.run(run())

    def test_services_reject_non_admin_owner(self):
        async def run():
            hass = FakeHass()
            await self.module.async_setup_voice_route(hass)
            call = SimpleNamespace(context=SimpleNamespace(user_id="guest"),
                                   data={"target": "phone"})
            with self.assertRaisesRegex(RuntimeError, "not_allowed"):
                await hass.services.handlers[("habab_home", "select_voice_route")](call)

        asyncio.run(run())

    def test_registered_wake_service_defers_on_and_off_until_voice_ends(self):
        async def run():
            hass = FakeHass()
            await self.module.async_setup_voice_route(hass)
            mailbox = hass.data["habab_home"]["voice_route"]
            call = SimpleNamespace(context=SimpleNamespace(user_id="owner"), data={"enabled": True})
            mailbox.poll("owner", {"safe_to_switch": False, "active_voice": True,
                "wake_enabled": False, "headset_available": True,
                "phase": "thinking", "selected_target": "phone",
                "actual_route": "phone", "input_type": 15})
            handler = hass.services.handlers[("habab_home", "set_voice_wake")]
            on_result = await handler(call)
            self.assertEqual(on_result["status"], "queued")
            self.assertIn("после сигнала окончания", on_result["message"])

            mailbox.poll("owner", {"safe_to_switch": True, "active_voice": False,
                "wake_enabled": True, "headset_available": True, "phase": "wake",
                "selected_target": "phone", "actual_route": "phone", "input_type": 15}, {
                "id": on_result["command_id"], "result": "applied", "actual_route": "phone",
                "input_type": 15, "wake_enabled": True, "error": "",
            })
            call.data = {"enabled": False}
            mailbox.poll("owner", {"safe_to_switch": False, "active_voice": True,
                "wake_enabled": True, "headset_available": True,
                "phase": "thinking", "selected_target": "phone",
                "actual_route": "phone", "input_type": 15})
            off_result = await handler(call)
            self.assertEqual(off_result["status"], "queued")
            self.assertIn("после сигнала окончания", off_result["message"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
