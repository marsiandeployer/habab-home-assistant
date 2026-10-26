"""Native writes are mocked: protect scope, consent, exact receipts and cleanup."""
from copy import deepcopy
import inspect
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home import automation_setup as module
from homeassistant.components.habab_home.automation_setup import AutomationSetup
from homeassistant.components.habab_home.const import DOMAIN
from homeassistant.components.automation.config import _backward_compat_schema


def config():
    return {
        "alias": "QA trigger to target", "triggers": [{"trigger": "state", "entity_id": "input_boolean.qa_trigger", "to": "on"}],
        "conditions": [], "actions": [{"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.qa_target"}}],
        "mode": "single",
    }


class AutomationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = SimpleNamespace(
            auth=SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=True, is_active=True))),
            data={DOMAIN: {"assistant": SimpleNamespace(web_sessions={"alice": "web-one", "bob": "web-two"})}},
            states=SimpleNamespace(get=lambda entity_id: self.states.get(entity_id)),
        )
        self.states = {}
        self.registry = SimpleNamespace(entities={}, async_get_entity_id=lambda kind, platform, native_id: self.entities.get(native_id))
        self.entities = {}
        self.registry_patch = patch.object(module.entity_registry, "async_get", return_value=self.registry)
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)
        self.validator = patch.object(module, "async_validate_config_item", new_callable=AsyncMock)
        self.validate = self.validator.start()
        self.addCleanup(self.validator.stop)
        self.controller = AutomationSetup(self.hass)
        self.journal = {"drafts": []}
        async def save(data):
            self.journal = deepcopy(data)
        self.store = SimpleNamespace(async_save=AsyncMock(side_effect=save),
                                     async_load=AsyncMock(side_effect=lambda: deepcopy(self.journal)))
        self.controller._store = self.store
        self.automations = []
        self.helpers = []
        self.controller._automations = AsyncMock(side_effect=lambda: deepcopy(self.automations))
        self.controller._helpers = AsyncMock(side_effect=lambda: deepcopy(self.helpers))

    async def draft(self, **overrides):
        return await self.controller.draft("alice", "web-one", {
            "action": "automation_draft", "config": config(), "summary": "Turn target on when trigger turns on.", **overrides})

    async def saved(self):
        draft = await self.draft()
        await self.controller.prepare("alice", "web-one", draft["draft_id"], draft["config_hash"])
        self.automations.append(deepcopy(draft["config"]))
        native_id = draft["config"]["id"]
        self.entities[native_id] = "automation.qa"
        self.states["automation.qa"] = SimpleNamespace(state="off")
        return await self.controller.verify("alice", "web-one", draft["draft_id"])

    async def test_draft_is_validated_immutable_forced_disabled_without_writing(self):
        request = config()
        request["initial_state"] = True
        draft = await self.draft(config=request)
        self.assertIs(draft["config"]["initial_state"], False)
        self.assertEqual(draft["phase"], "draft")
        self.validate.assert_awaited_once()
        self.controller._automations.assert_not_awaited()
        draft["config"]["actions"] = []
        request["alias"] = "Changed"
        stored = (await self.controller.status("alice", "web-one"))["drafts"][0]
        self.assertEqual(stored["config"]["actions"], config()["actions"])
        self.assertEqual(stored["config"]["alias"], config()["alias"])

    async def test_core_mutating_legacy_key_normalization_is_frozen_before_hash(self):
        legacy = config()
        for old, new in (("trigger", "triggers"), ("action", "actions"), ("condition", "conditions")):
            legacy[old] = legacy.pop(new)
        self.validate.side_effect = lambda _hass, _key, data: _backward_compat_schema(data)
        draft = await self.draft(config=legacy)
        for old, new in (("trigger", "triggers"), ("action", "actions"), ("condition", "conditions")):
            self.assertNotIn(old, draft["config"])
            self.assertEqual(draft["config"][new], legacy[old])
        self.assertEqual(draft["config_hash"], module._digest(draft["config"]))
        self.assertIn("trigger", legacy)  # Caller/model request is not mutated.

    async def test_issued_legacy_draft_recovers_native_normalization_without_rewriting_approval(self):
        draft = await self.draft()
        record = self.controller.drafts[draft["draft_id"]]
        approved = deepcopy(draft["config"])
        for old, new in (("trigger", "triggers"), ("action", "actions"), ("condition", "conditions")):
            approved[old] = approved.pop(new)
        record["config_json"] = module._json(approved)
        record["config_hash"] = module._digest(approved)
        original_hash, original_bytes = record["config_hash"], record["config_json"]
        await self.controller.prepare("alice", "web-one", draft["draft_id"], original_hash)
        native = _backward_compat_schema(deepcopy(approved))
        self.automations.append(native)
        self.entities[native["id"]] = "automation.qa"
        self.states["automation.qa"] = SimpleNamespace(state="off")
        self.validate.side_effect = lambda _hass, _key, data: _backward_compat_schema(data)
        saved = await self.controller.verify("alice", "web-one", draft["draft_id"])
        self.assertEqual(saved["phase"], "saved")
        self.assertEqual(saved["config_hash"], original_hash)
        self.assertEqual(saved["native_config_hash"], module._digest(native))
        self.assertEqual(saved["native_normalization"], "core_automation_schema")
        self.assertEqual(record["config_json"], original_bytes)
        await self.controller.cleanup_prepare("alice", "web-one", draft["draft_id"], original_hash)
        self.automations[0]["actions"] = []
        with self.assertRaisesRegex(ValueError, "readback_mismatch"):
            await self.controller._readback(record)

    async def test_model_cannot_put_confirmation_or_owner_fields_in_draft(self):
        for extra in ({"confirm": True}, {"owner": "bob"}, {"session_id": "web-two"}):
            with self.assertRaisesRegex(ValueError, "invalid_draft"):
                await self.draft(**extra)
        self.assertFalse(self.controller.drafts)

    async def test_invalid_native_config_never_becomes_a_draft(self):
        self.validate.side_effect = ValueError("invalid native schema")
        with self.assertRaisesRegex(ValueError, "invalid_draft"):
            await self.draft()
        self.assertFalse(self.controller.drafts)

    async def test_acl_inactive_and_non_admin(self):
        for user in (None, SimpleNamespace(is_admin=False, is_active=True), SimpleNamespace(is_admin=True, is_active=False)):
            self.hass.auth.async_get_user.return_value = user
            with self.assertRaisesRegex(ValueError, "unauthorized"):
                await self.draft()

    async def test_current_conversation_and_owner_are_required_for_all_operations(self):
        draft = await self.draft()
        with self.assertRaisesRegex(ValueError, "unknown_draft"):
            await self.controller.prepare("bob", "web-two", draft["draft_id"], draft["config_hash"])
        self.hass.data[DOMAIN]["assistant"].web_sessions["alice"] = "new-web"
        operations = [
            self.controller.status("alice", "web-one"),
            self.controller.prepare("alice", "web-one", draft["draft_id"], draft["config_hash"]),
            self.controller.verify("alice", "web-one", draft["draft_id"]),
            self.controller.cancel("alice", "web-one", draft["draft_id"]),
            self.controller.cleanup_prepare("alice", "web-one", draft["draft_id"], draft["config_hash"]),
            self.controller.cleanup_verify("alice", "web-one", draft["draft_id"]),
        ]
        for operation in operations:
            with self.assertRaisesRegex(ValueError, "stale_session"):
                await operation
        self.assertEqual((await self.controller.status("alice", "new-web"))["drafts"], [])

    async def test_session_changed_while_validation_is_awaiting_is_rejected(self):
        async def validate(*_args):
            self.hass.data[DOMAIN]["assistant"].web_sessions["alice"] = "new-web"
        self.validate.side_effect = validate
        with self.assertRaisesRegex(ValueError, "stale_session"):
            await self.draft()
        self.assertFalse(self.controller.drafts)

    async def test_forged_hash_and_unconfirmed_verify_cannot_save(self):
        draft = await self.draft()
        with self.assertRaisesRegex(ValueError, "invalid_draft"):
            await self.controller.prepare("alice", "web-one", draft["draft_id"], "0" * 64)
        with self.assertRaisesRegex(ValueError, "confirmation_required"):
            await self.controller.verify("alice", "web-one", draft["draft_id"])

    async def test_native_config_and_disabled_state_both_required(self):
        draft = await self.draft()
        await self.controller.prepare("alice", "web-one", draft["draft_id"], draft["config_hash"])
        result = await self.controller.verify("alice", "web-one", draft["draft_id"])
        self.assertEqual(result["phase"], "saving")
        self.assertNotIn("verification", result)
        self.automations.append(deepcopy(draft["config"]))
        self.entities[draft["config"]["id"]] = "automation.qa"
        for state in ("on", "unavailable"):
            self.states["automation.qa"] = SimpleNamespace(state=state)
            result = await self.controller.verify("alice", "web-one", draft["draft_id"])
            self.assertEqual(result["phase"], "saving")
        self.states["automation.qa"] = SimpleNamespace(state="off")
        result = await self.controller.verify("alice", "web-one", draft["draft_id"])
        self.assertEqual(result["verification"], "native_config_and_off_state")
        self.assertEqual(result["config_hash"], draft["config_hash"])

    async def test_mismatched_persisted_config_is_not_a_receipt(self):
        saved = await self.saved()
        self.automations[0]["actions"] = []
        result = await self.controller.status("alice", "web-one")
        self.assertEqual(result["drafts"][0]["phase"], "error")
        self.assertNotIn("verification", result["drafts"][0])
        with self.assertRaisesRegex(ValueError, "readback_mismatch"):
            await self.controller.cleanup_prepare("alice", "web-one", saved["draft_id"], saved["config_hash"])

    async def test_lost_post_response_reconciles_and_never_prepares_twice(self):
        saved = await self.saved()
        with self.assertRaisesRegex(ValueError, "draft_not_pending"):
            await self.controller.prepare("alice", "web-one", saved["draft_id"], saved["config_hash"])
        result = await self.controller.status("alice", "web-one")
        self.assertEqual(result["drafts"][0]["phase"], "saved")

    async def test_cleanup_only_exact_created_still_off_object_and_absent_state(self):
        saved = await self.saved()
        self.automations.append({"id": "preexisting", "alias": "Keep"})
        self.states["automation.qa"] = SimpleNamespace(state="on")
        with self.assertRaisesRegex(ValueError, "cleanup_not_allowed"):
            await self.controller.cleanup_prepare("alice", "web-one", saved["draft_id"], saved["config_hash"])
        self.states["automation.qa"] = SimpleNamespace(state="off")
        await self.controller.cleanup_prepare("alice", "web-one", saved["draft_id"], saved["config_hash"])
        self.automations.pop(0)
        pending = await self.controller.cleanup_verify("alice", "web-one", saved["draft_id"])
        self.assertEqual(pending["phase"], "deleting")
        self.states.pop("automation.qa")
        result = await self.controller.cleanup_verify("alice", "web-one", saved["draft_id"])
        self.assertEqual(result["verification"], "native_absence_readback")
        self.assertEqual(self.automations, [{"id": "preexisting", "alias": "Keep"}])

    async def test_two_native_helper_drafts_return_exact_created_entity_ids(self):
        self.helpers.append({"id": "keep", "name": "Existing", "initial": False})
        saved_helpers = []
        for index in (1, 2):
            name = f"QA helper {index}"
            draft = await self.controller.draft("alice", "web-one", {
                "action": "automation_helper_draft", "name": name, "summary": "Disposable virtual toggle."}, helper=True)
            await self.controller.prepare("alice", "web-one", draft["draft_id"], draft["config_hash"])
            pending = await self.controller.verify("alice", "web-one", draft["draft_id"])
            self.assertEqual(pending["phase"], "saving")
            helper_id = f"qa_helper_{index}"
            self.helpers.append({"id": helper_id, **draft["config"]})
            self.entities[helper_id] = f"input_boolean.{helper_id}"
            self.states[f"input_boolean.{helper_id}"] = SimpleNamespace(state="off")
            saved = await self.controller.verify("alice", "web-one", draft["draft_id"])
            self.assertEqual(saved["entity_id"], f"input_boolean.{helper_id}")
            self.assertEqual(saved["phase"], "saved")
            saved_helpers.append(saved)
        for saved in saved_helpers:
            await self.controller.cleanup_prepare("alice", "web-one", saved["draft_id"], saved["config_hash"])
            self.helpers[:] = [item for item in self.helpers if item["id"] != saved["native_id"]]
            self.states.pop(saved["entity_id"])
            deleted = await self.controller.cleanup_verify("alice", "web-one", saved["draft_id"])
            self.assertEqual(deleted["phase"], "deleted")
        self.assertEqual(self.helpers, [{"id": "keep", "name": "Existing", "initial": False}])

    async def test_existing_helper_cannot_be_adopted_for_cleanup(self):
        draft = await self.controller.draft("alice", "web-one", {
            "action": "automation_helper_draft", "name": "Existing", "summary": "Existing toggle."}, helper=True)
        self.helpers.append({"id": "existing", **draft["config"]})
        with self.assertRaisesRegex(ValueError, "already_exists"):
            await self.controller.prepare("alice", "web-one", draft["draft_id"], draft["config_hash"])

    async def test_journal_persisted_before_permit_and_failure_never_returns_permit(self):
        draft = await self.draft()
        approved = await self.controller.prepare("alice", "web-one", draft["draft_id"], draft["config_hash"])
        persisted = self.journal["drafts"][0]
        self.assertEqual(persisted["phase"], "saving")
        self.assertEqual(persisted["config_hash"], approved["config_hash"])
        other = await self.draft()
        self.store.async_save.side_effect = OSError("mock disk unavailable")
        with self.assertRaisesRegex(ValueError, "native_api_unavailable"):
            await self.controller.prepare("alice", "web-one", other["draft_id"], other["config_hash"])

    async def test_restart_new_chat_recovers_owner_cleanup_but_never_old_confirmation(self):
        saved = await self.saved()
        old_draft = await self.draft()
        self.hass.data[DOMAIN]["assistant"].web_sessions["alice"] = "new-web"
        restored = AutomationSetup(self.hass)
        with patch.object(module, "Store", return_value=self.store):
            await restored.setup()
        restored._automations = self.controller._automations
        restored._helpers = self.controller._helpers
        self.assertEqual((await restored.status("alice", "new-web"))["drafts"], [])
        recovered = (await restored.ui_status("alice", "new-web"))["recovery_drafts"]
        self.assertEqual([item["draft_id"] for item in recovered], [saved["draft_id"]])
        self.assertTrue(recovered[0]["from_previous_chat"])
        self.assertEqual(restored.drafts[saved["draft_id"]]["session_id"], "web-one")
        with self.assertRaisesRegex(ValueError, "unknown_draft"):
            await restored.prepare("alice", "new-web", old_draft["draft_id"], old_draft["config_hash"])
        self.assertEqual((await restored.ui_status("bob", "web-two"))["recovery_drafts"], [])
        await restored.cleanup_prepare("alice", "new-web", saved["draft_id"], saved["config_hash"])
        self.automations.clear()
        self.states.clear()
        deleted = await restored.cleanup_verify("alice", "new-web", saved["draft_id"])
        self.assertEqual(deleted["phase"], "deleted")

    async def test_helper_ack_binds_native_id_and_never_adopts_same_name_other_object(self):
        draft = await self.controller.draft("alice", "web-one", {
            "action": "automation_helper_draft", "name": "QA", "summary": "Virtual helper."}, helper=True)
        self.assertTrue(draft["config"]["name"].endswith(draft["draft_id"][:8]))
        await self.controller.prepare("alice", "web-one", draft["draft_id"], draft["config_hash"])
        await self.controller.acknowledge("alice", "web-one", draft["draft_id"], "native_qa")
        self.helpers.append({"id": "other_qa", **draft["config"]})
        self.entities["other_qa"] = "input_boolean.other_qa"
        self.states["input_boolean.other_qa"] = SimpleNamespace(state="off")
        result = await self.controller.verify("alice", "web-one", draft["draft_id"])
        self.assertEqual(result["phase"], "saving")
        self.assertNotIn("verification", result)
        self.helpers.append({"id": "native_qa", **draft["config"]})
        self.entities["native_qa"] = "input_boolean.native_qa"
        self.states["input_boolean.native_qa"] = SimpleNamespace(state="off")
        result = await self.controller.verify("alice", "web-one", draft["draft_id"])
        self.assertEqual(result["entity_id"], "input_boolean.native_qa")

    async def test_absent_intent_dismissal_waits_then_keeps_late_result_recoverable(self):
        draft = await self.draft()
        await self.controller.prepare("alice", "web-one", draft["draft_id"], draft["config_hash"])
        with self.assertRaisesRegex(ValueError, "cleanup_not_allowed"):
            await self.controller.dismiss_absent("alice", "web-one", draft["draft_id"])
        self.controller.drafts[draft["draft_id"]]["prepared"] = time.time() - 31
        result = await self.controller.dismiss_absent("alice", "web-one", draft["draft_id"])
        self.assertEqual(result["phase"], "dismissed")
        self.automations.append(deepcopy(draft["config"]))
        self.entities[draft["config"]["id"]] = "automation.late"
        self.states["automation.late"] = SimpleNamespace(state="off")
        self.hass.data[DOMAIN]["assistant"].web_sessions["alice"] = "new-web"
        result = await self.controller.ui_status("alice", "new-web")
        self.assertEqual(result["recovery_drafts"][0]["phase"], "saved")
        self.assertEqual(result["recovery_drafts"][0]["draft_id"], draft["draft_id"])

    async def test_enabled_object_loses_disabled_receipt_and_changed_object_does_not_hide_other_drafts(self):
        saved = await self.saved()
        self.states["automation.qa"] = SimpleNamespace(state="on")
        result = (await self.controller.status("alice", "web-one"))["drafts"][0]
        self.assertEqual(result["phase"], "saving")
        self.assertNotIn("verification", result)
        other = await self.draft()
        self.automations[0]["alias"] = "Changed externally"
        result = await self.controller.ui_status("alice", "web-one")
        self.assertEqual(result["drafts"][0]["phase"], "error")
        self.assertEqual(result["drafts"][1]["draft_id"], other["draft_id"])

    async def test_session_changed_during_journal_write_returns_no_approval(self):
        draft = await self.draft()
        async def save(_data):
            self.hass.data[DOMAIN]["assistant"].web_sessions["alice"] = "new-web"
        self.store.async_save.side_effect = save
        with self.assertRaisesRegex(ValueError, "stale_session"):
            await self.controller.prepare("alice", "web-one", draft["draft_id"], draft["config_hash"])

    async def test_expired_draft_cannot_be_confirmed(self):
        draft = await self.draft()
        self.controller.drafts[draft["draft_id"]]["created"] -= module.DRAFT_TTL + 1
        with self.assertRaisesRegex(ValueError, "draft_not_pending"):
            await self.controller.prepare("alice", "web-one", draft["draft_id"], draft["config_hash"])

    async def test_ws_rejects_inactive_or_non_admin_before_dispatch(self):
        for user in (None, SimpleNamespace(id="alice", is_active=False, is_admin=True),
                     SimpleNamespace(id="alice", is_active=True, is_admin=False)):
            connection = SimpleNamespace(user=user, send_error=Mock(), send_result=Mock())
            await inspect.unwrap(module.automation_action)(self.hass, connection, {
                "id": 1, "operation": "prepare", "session_id": "web-one"})
            connection.send_error.assert_called_once_with(1, "unauthorized", "unauthorized")
            connection.send_result.assert_not_called()


if __name__ == "__main__":
    unittest.main()
