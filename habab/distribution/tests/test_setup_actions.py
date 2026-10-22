"""Native action bridge: flow ownership and secret-free form serialization."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from homeassistant.components.habab_home.setup_actions import execute, flow_result


class ActionBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_flow_result_never_returns_entry_credentials_or_defaults(self):
        result = flow_result({"type": "create_entry", "flow_id": "flow-one", "title": "Private",
            "data": {"api_key": "fixture-private-key"}, "result": object(),
            "description_placeholders": {"token": "fixture-private-token"}})
        self.assertEqual(result, {"type": "create_entry", "flow_id": "flow-one", "fields": []})

    async def test_another_owner_cannot_continue_a_configuration_flow(self):
        configure = AsyncMock()
        hass = SimpleNamespace(config_entries=SimpleNamespace(flow=SimpleNamespace(async_configure=configure)))
        manager = SimpleNamespace(flow_owners={"flow-one": ("alice", "config")})
        with self.assertRaises(ValueError):
            await execute(hass, manager, "bob", {"action": "continue_integration", "flow_id": "flow-one", "input": {}})
        configure.assert_not_awaited()

    async def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            await execute(None, None, "alice", {"action": "shell", "command": "anything"})
