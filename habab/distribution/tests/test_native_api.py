"""Native API parity keeps HA authorization and revokes temporary credentials."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home.native_api import native_api, validate_request


class NativeApiTests(unittest.IsolatedAsyncioTestCase):
    def setup_case(self):
        user = SimpleNamespace(is_active=True, is_admin=True)
        auth = SimpleNamespace(async_get_user=AsyncMock(return_value=user),
            async_create_refresh_token=AsyncMock(return_value=object()),
            async_create_access_token=Mock(return_value="private-native-token"),
            async_remove_refresh_token=Mock())
        return SimpleNamespace(auth=auth), SimpleNamespace(active=("owner", "session"))

    async def test_native_commands_are_not_restricted_to_custom_tool_catalog(self):
        for command in ["config/area_registry/update", "config/device_registry/update",
                        "lovelace/config/save", "config_entries/flow/progress"]:
            hass, manager = self.setup_case()
            ws = SimpleNamespace(receive_json=AsyncMock(side_effect=[
                {"type": "auth_required"}, {"type": "auth_ok"},
                {"type": "result", "id": 1, "success": True, "result": {"persisted": True}}]),
                send_json=AsyncMock())
            @asynccontextmanager
            async def connect(*args, **kwargs):
                yield ws
            with patch("homeassistant.components.habab_home.native_api.async_get_clientsession",
                       return_value=SimpleNamespace(ws_connect=connect)):
                result = await native_api(hass, manager, "owner", {
                    "action": "native_api", "protocol": "websocket", "message": {"type": command}})
            self.assertTrue(result["ok"])
            self.assertEqual(ws.send_json.await_args_list[-1].args[0], {"type": command, "id": 1})
            self.assertNotIn("private-native-token", str(result))
            hass.auth.async_remove_refresh_token.assert_called_once()

    async def test_failed_native_command_preserves_actual_native_error(self):
        hass, manager = self.setup_case()
        ws = SimpleNamespace(receive_json=AsyncMock(side_effect=[
            {"type": "auth_required"}, {"type": "auth_ok"},
            {"type": "result", "id": 1, "success": False,
             "error": {"code": "unauthorized", "message": "Owner permission required"}}]), send_json=AsyncMock())
        @asynccontextmanager
        async def connect(*args, **kwargs):
            yield ws
        with patch("homeassistant.components.habab_home.native_api.async_get_clientsession",
                   return_value=SimpleNamespace(ws_connect=connect)):
            r = await native_api(hass, manager, "owner", {"protocol": "websocket", "message": {"type": "config/core/update"}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["result"]["code"], "unauthorized")
        hass.auth.async_remove_refresh_token.assert_called_once()

    async def test_cancelled_request_revokes_native_credential(self):
        import asyncio
        hass, manager = self.setup_case()
        @asynccontextmanager
        async def connect(*args, **kwargs):
            raise asyncio.CancelledError
            yield
        with patch("homeassistant.components.habab_home.native_api.async_get_clientsession",
                   return_value=SimpleNamespace(ws_connect=connect)):
            with self.assertRaises(asyncio.CancelledError):
                await native_api(hass, manager, "owner", {"protocol": "websocket", "message": {"type": "get_config"}})
        hass.auth.async_remove_refresh_token.assert_called_once()

    async def test_inactive_or_nonadmin_cannot_issue_native_credentials(self):
        hass, manager = self.setup_case()
        manager.active = None
        with self.assertRaisesRegex(ValueError, "inactive"):
            await native_api(hass, manager, "owner", {"protocol": "rest", "path": "/api/config"})
        manager.active = ("owner", "session")
        hass.auth.async_get_user.return_value.is_admin = False
        with self.assertRaisesRegex(ValueError, "not_allowed"):
            await native_api(hass, manager, "owner", {"protocol": "rest", "path": "/api/config"})
        hass.auth.async_create_refresh_token.assert_not_awaited()

    def test_external_urls_and_caller_auth_are_rejected(self):
        for path in ["https://example.com/api/config", "//example.com/api/config", "/auth/token"]:
            with self.assertRaises(ValueError):
                validate_request({"protocol": "rest", "path": path})
        with self.assertRaises(ValueError):
            validate_request({"protocol": "websocket", "message": {"type": "get_config", "id": 2}})

    async def test_rest_uses_owner_auth_and_preserves_native_validation_errors(self):
        hass, manager = self.setup_case()
        class Content:
            async def iter_chunked(self, size):
                yield b'{"message":"Invalid option"}'
        response = SimpleNamespace(status=400, headers={"Content-Type": "application/json"}, content=Content())
        captured = {}
        @asynccontextmanager
        async def request(method, url, **kwargs):
            captured.update(method=method, url=url, **kwargs)
            yield response
        with patch("homeassistant.components.habab_home.native_api.async_get_clientsession",
                   return_value=SimpleNamespace(request=request)):
            result = await native_api(hass, manager, "owner", {"protocol": "rest", "path": "/api/config/core/config",
                "method": "POST", "body": {"name": "test"}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 400)
        self.assertEqual(result["result"], {"message": "Invalid option"})
        self.assertFalse(captured["allow_redirects"])
        self.assertEqual(captured["headers"]["Authorization"], "Bearer private-native-token")
        hass.auth.async_remove_refresh_token.assert_called_once()

    async def test_binary_download_is_saved_without_putting_content_in_context(self):
        import tempfile
        from pathlib import Path
        from homeassistant.components.habab_home.native_api import _rest
        binary = b'private backup contents'
        class Content:
            async def iter_chunked(self, size):
                yield binary
        response = SimpleNamespace(status=200, headers={"Content-Type": "application/octet-stream"}, content=Content())
        @asynccontextmanager
        async def request(*args, **kwargs):
            yield response
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup.tar"
            result = await _rest(SimpleNamespace(request=request), "token", {
                "path": "/api/backup/download", "save_to": str(destination)})
            self.assertEqual(destination.read_bytes(), binary)
            self.assertEqual(result["encoding"], "file")
            self.assertNotIn(binary.decode(), str(result))
            with self.assertRaises(ValueError):
                await _rest(SimpleNamespace(request=request), "token", {
                    "path": "/api/backup/download", "save_to": str(destination)})
            self.assertEqual(destination.read_bytes(), binary)
