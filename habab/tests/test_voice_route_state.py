import unittest
import importlib.util
from pathlib import Path

STATE_PATH = Path(__file__).parents[1] / "distribution/src/habab_home/voice_route_state.py"
SPEC = importlib.util.spec_from_file_location("voice_route_state", STATE_PATH)
voice_route_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(voice_route_state)
VoiceRouteMailbox = voice_route_state.VoiceRouteMailbox


class VoiceRouteMailboxTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.mailbox = VoiceRouteMailbox(clock=lambda: self.now)
        self.client = {
            "safe_to_switch": True, "active_voice": False, "wake_enabled": True,
            "headset_available": True,
            "phase": "wake", "selected_target": "phone", "actual_route": "phone",
            "input_type": 15,
        }

    def test_command_is_owner_scoped_and_delivered_only_at_safe_boundary(self):
        self.mailbox.poll("owner-a", self.client)
        command_id, completed = self.mailbox.enqueue("owner-a", "bluetooth")
        unsafe = {**self.client, "safe_to_switch": False, "active_voice": True}
        self.assertIsNone(self.mailbox.poll("owner-a", unsafe)["command"])
        self.assertIsNone(self.mailbox.poll("owner-b", self.client)["command"])
        command = self.mailbox.poll("owner-a", self.client)["command"]
        self.assertEqual(command, {"id": command_id, "target": "bluetooth"})
        self.assertIsNone(self.mailbox.poll("owner-a", self.client)["command"])
        self.assertFalse(completed.is_set())

    def test_ack_requires_matching_requested_and_actual_route(self):
        self.mailbox.poll("owner-a", self.client)
        command_id, completed = self.mailbox.enqueue("owner-a", "bluetooth")
        self.mailbox.poll("owner-a", self.client)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.mailbox.poll("owner-a", {**self.client, "actual_route": "phone"}, {
                "id": command_id, "result": "applied", "actual_route": "phone",
                "input_type": 15, "wake_enabled": True, "error": "",
            })
        result = self.mailbox.poll("owner-a", {
            **self.client, "actual_route": "bluetooth", "input_type": 7,
            "selected_target": "bluetooth",
        }, {"id": command_id, "result": "applied", "actual_route": "bluetooth",
            "input_type": 7, "wake_enabled": True, "error": ""})
        self.assertTrue(completed.is_set())
        self.assertEqual(result["status"]["last_result"]["input_type"], 7)

    def test_wake_on_and_off_acknowledge_the_actual_enabled_state(self):
        self.mailbox.poll("owner-a", self.client)
        on_id, on_done = self.mailbox.enqueue("owner-a", "wake_on")
        self.mailbox.poll("owner-a", self.client)
        on_client = {**self.client, "wake_enabled": True, "phase": "wake"}
        self.mailbox.poll("owner-a", on_client, {
            "id": on_id, "result": "applied", "actual_route": "phone",
            "input_type": 15, "wake_enabled": True, "error": "",
        })
        self.assertTrue(on_done.is_set())
        self.mailbox.poll("owner-a", {**on_client, "safe_to_switch": True})
        off_id, off_done = self.mailbox.enqueue("owner-a", "wake_off")
        self.mailbox.poll("owner-a", {**on_client, "safe_to_switch": True})
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.mailbox.poll("owner-a", {**self.client, "wake_enabled": False}, {
                "id": off_id, "result": "applied", "actual_route": "off",
                "input_type": -1, "wake_enabled": True, "error": "",
            })
        result = self.mailbox.poll("owner-a", {**self.client, "wake_enabled": False}, {
            "id": off_id, "result": "applied", "actual_route": "off",
            "input_type": -1, "wake_enabled": False, "error": "",
        })
        self.assertTrue(off_done.is_set())
        self.assertFalse(result["status"]["last_result"]["wake_enabled"])

    def test_stale_client_and_unknown_target_fail_closed(self):
        self.mailbox.poll("owner-a", self.client)
        self.now += 9
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.mailbox.enqueue("owner-a", "phone")
        self.now = 10
        with self.assertRaisesRegex(ValueError, "invalid_voice_route_target"):
            self.mailbox.enqueue("owner-a", "speaker")

    def test_explicit_bluetooth_and_toggle_fail_early_without_a_headset(self):
        self.mailbox.poll("owner-a", {**self.client, "headset_available": False})
        with self.assertRaisesRegex(ValueError, "headset_unavailable"):
            self.mailbox.enqueue("owner-a", "bluetooth")
        with self.assertRaisesRegex(ValueError, "headset_unavailable"):
            self.mailbox.enqueue("owner-a", "toggle")
        self.mailbox.enqueue("owner-a", "phone")

    def test_lost_delivery_is_retried_with_a_lease_then_expires(self):
        self.mailbox.poll("owner-a", self.client)
        command_id, completed = self.mailbox.enqueue("owner-a", "phone")
        first = self.mailbox.poll("owner-a", self.client)["command"]
        self.assertEqual(first["id"], command_id)
        self.assertIsNone(self.mailbox.poll("owner-a", self.client)["command"])
        self.now += 7
        retry = self.mailbox.poll("owner-a", self.client)["command"]
        self.assertEqual(retry["id"], command_id)
        self.now += 46
        status = self.mailbox.status("owner-a")
        self.assertFalse(status["pending"])
        self.assertEqual(status["last_result"]["error"], "route_command_timeout")
        self.assertTrue(completed.is_set())

    def test_lost_ack_response_is_idempotent_and_does_not_consume_next_command(self):
        self.mailbox.poll("owner-a", self.client)
        first_id, _ = self.mailbox.enqueue("owner-a", "phone")
        self.mailbox.poll("owner-a", self.client)
        first_ack = {"id": first_id, "result": "applied", "actual_route": "phone",
                     "input_type": 15, "wake_enabled": True, "error": ""}
        self.mailbox.poll("owner-a", self.client, first_ack)

        second_id, _ = self.mailbox.enqueue("owner-a", "bluetooth")
        retried = self.mailbox.poll("owner-a", self.client, first_ack)
        self.assertTrue(retried["ack_recorded"])
        self.assertEqual(retried["command"], {"id": second_id, "target": "bluetooth"})
        self.assertTrue(retried["status"]["pending"])

    def test_ack_from_before_mailbox_restart_is_discardable_and_keeps_new_command(self):
        stale_ack = {"id": "before-restart", "result": "applied", "actual_route": "phone",
                     "input_type": 15, "wake_enabled": True, "error": ""}
        # A new mailbox models HA's in-memory state being lost on restart.
        restarted = VoiceRouteMailbox(clock=lambda: self.now)
        restarted.poll("owner-a", self.client)
        new_id, _ = restarted.enqueue("owner-a", "bluetooth")

        response = restarted.poll("owner-a", self.client, stale_ack)
        self.assertTrue(response["ack_unknown"])
        self.assertEqual(response["ack_unknown_id"], "before-restart")
        self.assertFalse(response["ack_recorded"])
        self.assertEqual(response["command"], {"id": new_id, "target": "bluetooth"})
        self.assertTrue(response["status"]["pending"])


if __name__ == "__main__":
    unittest.main()
