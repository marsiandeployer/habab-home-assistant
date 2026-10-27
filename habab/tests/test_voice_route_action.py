import asyncio
import importlib.util
from pathlib import Path
import unittest

ACTION_PATH = Path(__file__).parents[1] / "distribution/src/habab_home/voice_route_action.py"
SPEC = importlib.util.spec_from_file_location("voice_route_action", ACTION_PATH)
voice_route_action = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(voice_route_action)


class FakeUser:
    is_active = True
    is_admin = True


class FakeAuth:
    async def async_get_user(self, owner):
        return FakeUser()


class FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, **kwargs):
        self.calls.append((domain, service, data, kwargs))
        return getattr(self, "response", {"status": "queued", "command_id": "safe-id",
                    "message": "Переключу после завершения текущего диалога."})


class FakeHass:
    def __init__(self):
        self.auth = FakeAuth()
        self.services = FakeServices()


class VoiceRouteActionTests(unittest.TestCase):
    def test_action_returns_real_service_response_and_admin_context(self):
        hass = FakeHass()
        result = asyncio.run(voice_route_action.call_select_voice_route(
            hass, "admin-id", "bluetooth", lambda **kwargs: kwargs))
        self.assertEqual(result["status"], "queued")
        domain, service, data, kwargs = hass.services.calls[0]
        self.assertEqual((domain, service, data),
                         ("habab_home", "select_voice_route", {"target": "bluetooth"}))
        self.assertTrue(kwargs["blocking"])
        self.assertTrue(kwargs["return_response"])
        self.assertEqual(kwargs["context"], {"user_id": "admin-id"})

    def test_action_rejects_arbitrary_targets_before_service_call(self):
        hass = FakeHass()
        with self.assertRaisesRegex(ValueError, "invalid_voice_route_target"):
            asyncio.run(voice_route_action.call_select_voice_route(
                hass, "admin-id", "shell", lambda **kwargs: kwargs))
        self.assertEqual(hass.services.calls, [])

    def test_action_does_not_translate_missing_service_response_to_success(self):
        hass = FakeHass()
        hass.services.response = None
        with self.assertRaisesRegex(ValueError, "invalid_voice_route_response"):
            asyncio.run(voice_route_action.call_select_voice_route(
                hass, "admin-id", "phone", lambda **kwargs: kwargs))

    def test_wake_action_is_explicit_and_uses_the_same_owner_scoped_response_path(self):
        hass = FakeHass()
        result = asyncio.run(voice_route_action.call_set_voice_wake(
            hass, "admin-id", True, lambda **kwargs: kwargs))
        self.assertEqual(result["status"], "queued")
        domain, service, data, kwargs = hass.services.calls[0]
        self.assertEqual((domain, service, data),
                         ("habab_home", "set_voice_wake", {"enabled": True}))
        self.assertTrue(kwargs["return_response"])

    def test_wake_action_rejects_non_boolean_or_arbitrary_operations(self):
        hass = FakeHass()
        with self.assertRaisesRegex(ValueError, "invalid_wake_state"):
            asyncio.run(voice_route_action.call_set_voice_wake(
                hass, "admin-id", "on", lambda **kwargs: kwargs))
        self.assertEqual(hass.services.calls, [])


if __name__ == "__main__":
    unittest.main()
