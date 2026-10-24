"""Regression coverage for truthful Home Assistant todo mutations."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from homeassistant.components.habab_home.setup_actions import execute


ENTITY_ID = "todo.shopping_list"


class TodoServices:
    def __init__(self, items=None, *, persist_add=True):
        self.items = list(items or [])
        self.persist_add = persist_add
        self.calls = []

    def has_service(self, domain, service):
        return (domain, service) in {("todo", "get_items"), ("todo", "add_item")}

    async def async_call(self, domain, service, data, *, target, blocking,
                         context, return_response=False):
        self.calls.append((domain, service, data, target, return_response))
        if service == "get_items":
            return {ENTITY_ID: {"items": deepcopy(self.items)}}
        if service == "add_item" and self.persist_add:
            self.items.append({
                "uid": "new-item-uid",
                "summary": data["item"],
                "status": "needs_action",
            })
        return None


def hass_with_todo(services):
    return SimpleNamespace(
        states=SimpleNamespace(get=lambda entity_id: object() if entity_id == ENTITY_ID else None),
        services=services,
    )


class ShoppingListActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_todo_items_reads_exact_entity_and_omits_internal_uid(self):
        services = TodoServices([{
            "uid": "private-item-id",
            "summary": "oats",
            "status": "needs_action",
        }])

        result = await execute(hass_with_todo(services), None, "owner", {
            "action": "todo_items", "entity_id": ENTITY_ID,
        })

        self.assertEqual(result, {
            "entity_id": ENTITY_ID,
            "items": [{"summary": "oats", "status": "needs_action"}],
        })
        self.assertEqual(services.calls[0][1], "get_items")
        self.assertTrue(services.calls[0][4])
        self.assertNotIn("private-item-id", repr(result))

    async def test_todo_items_can_read_items_without_optional_uids(self):
        services = TodoServices([{"summary": "oats", "status": "needs_action"}])

        result = await execute(hass_with_todo(services), None, "owner", {
            "action": "todo_items", "entity_id": ENTITY_ID,
        })

        self.assertEqual(result["items"], [{"summary": "oats", "status": "needs_action"}])

    async def test_todo_add_item_returns_persisted_only_after_new_uid_readback(self):
        services = TodoServices([{
            "uid": "existing-item-id",
            "summary": "tea",
            "status": "needs_action",
        }])

        result = await execute(hass_with_todo(services), None, "owner", {
            "action": "call_service",
            "domain": "todo",
            "service": "add_item",
            "target": {"entity_id": ENTITY_ID},
            "data": {"item": "oats"},
        })

        self.assertEqual(result, {
            "called": "todo.add_item",
            "verification": "persisted",
            "items": [{"entity_id": ENTITY_ID, "summary": "oats", "status": "needs_action"}],
        })
        self.assertEqual([call[1] for call in services.calls], ["get_items", "add_item", "get_items"])

    async def test_identical_existing_summary_does_not_fake_new_persisted_item(self):
        services = TodoServices([{
            "uid": "existing-item-id",
            "summary": "oats",
            "status": "needs_action",
        }], persist_add=False)

        with self.assertRaisesRegex(ValueError, "todo_add_item_not_verified"):
            await execute(hass_with_todo(services), None, "owner", {
                "action": "call_service",
                "domain": "todo",
                "service": "add_item",
                "target": {"entity_id": ENTITY_ID},
                "data": {"item": "oats"},
            })

        self.assertEqual([call[1] for call in services.calls], ["get_items", "add_item", "get_items"])

    async def test_add_item_is_not_verified_without_a_new_uid(self):
        services = TodoServices([{"summary": "oats", "status": "needs_action"}], persist_add=False)

        with self.assertRaisesRegex(ValueError, "todo_add_item_not_verified"):
            await execute(hass_with_todo(services), None, "owner", {
                "action": "call_service",
                "domain": "todo",
                "service": "add_item",
                "target": {"entity_id": ENTITY_ID},
                "data": {"item": "oats"},
            })

    async def test_legitimate_duplicate_summary_is_verified_by_a_new_uid(self):
        services = TodoServices([{
            "uid": "existing-item-id",
            "summary": "oats",
            "status": "needs_action",
        }])

        result = await execute(hass_with_todo(services), None, "owner", {
            "action": "call_service",
            "domain": "todo",
            "service": "add_item",
            "target": {"entity_id": ENTITY_ID},
            "data": {"item": "oats"},
        })

        self.assertEqual(result["verification"], "persisted")
        self.assertEqual(len(result["items"]), 1)

    async def test_todo_add_item_requires_exact_entity_target_before_mutation(self):
        services = TodoServices()

        with self.assertRaisesRegex(ValueError, "todo_entity_target_required"):
            await execute(hass_with_todo(services), None, "owner", {
                "action": "call_service",
                "domain": "todo",
                "service": "add_item",
                "target": {},
                "data": {"item": "oats"},
            })

        self.assertEqual(services.calls, [])


if __name__ == "__main__":
    unittest.main()
