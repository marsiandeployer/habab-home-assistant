"""Local capability bridge for an already authorized administrator's OpenCode turn."""
import asyncio
from itertools import islice
import json
import logging
import secrets

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import DISCOVERY_SOURCES
from homeassistant.core import Context
from homeassistant.helpers import device_registry, entity_registry
from .const import DOMAIN
from .assistant_status import get_assistant_status

_LOGGER = logging.getLogger(__name__)


async def _todo_items(hass, owner, entity_id):
    if (not isinstance(entity_id, str) or not entity_id.startswith("todo.")
            or hass.states.get(entity_id) is None):
        raise ValueError("todo_entity_not_found")
    if not hass.services.has_service("todo", "get_items"):
        raise ValueError("todo_get_items_unavailable")
    async with asyncio.timeout(5):
        response = await hass.services.async_call(
            "todo", "get_items", {"status": ["needs_action", "completed"]},
            target={"entity_id": entity_id}, blocking=True, return_response=True,
            context=Context(user_id=owner),
        )
    if not isinstance(response, dict):
        raise ValueError("todo_items_unavailable")
    entity_response = response.get(entity_id)
    items = entity_response.get("items") if isinstance(entity_response, dict) else None
    if not isinstance(items, list):
        raise ValueError("todo_items_unavailable")
    valid_items = []
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("summary"), str)
                or (item.get("uid") is not None and not isinstance(item.get("uid"), str))
                or (item.get("status") is not None and not isinstance(item.get("status"), str))):
            raise ValueError("todo_items_unavailable")
        valid_items.append({
            "uid": item.get("uid"), "summary": item["summary"], "status": item.get("status")
        })
    return valid_items


def _todo_entity_ids(target):
    entity_ids = target.get("entity_id")
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    if (not isinstance(entity_ids, list) or not entity_ids
            or any(not isinstance(entity_id, str) or not entity_id.startswith("todo.")
                   for entity_id in entity_ids)):
        raise ValueError("todo_entity_target_required")
    return list(dict.fromkeys(entity_ids))


def flow_result(result):
    """Return only the flow control summary allowed in model context."""
    result_type = result.get("type")
    phase = {"form": "form", "menu": "menu", "progress": "progress",
             "progress_done": "progress", "external": "external",
             "create_entry": "progress", "abort": "abort"}.get(result_type, "error")
    summary = {"phase": phase}
    for key in ("flow_id", "step_id"):
        if isinstance(result.get(key), str):
            summary[key] = result[key]
    options = result.get("menu_options")
    if isinstance(options, (list, tuple)) and all(isinstance(item, str) for item in options):
        summary["menu_options"] = list(options)
    return summary


def discovered_flows(hass, manager):
    """Return safe summaries for unowned native discovery flows only."""
    flows = []
    for flow in hass.config_entries.flow.async_progress():
        context = flow.get("context")
        flow_id = flow.get("flow_id")
        if (
            not isinstance(context, dict)
            or context.get("source") not in DISCOVERY_SOURCES
            or not isinstance(flow_id, str)
            or flow_id in manager.flow_owners
        ):
            continue
        summary = {
            "flow_id": flow_id,
            "domain": flow.get("handler"),
            "step_id": flow.get("step_id"),
        }
        if isinstance(flow.get("title"), str):
            summary["title"] = flow["title"]
        menu_options = flow.get("menu_options")
        if isinstance(menu_options, (list, tuple, set)) and all(
            isinstance(option, str) for option in menu_options
        ):
            summary["menu_options"] = sorted(menu_options)
        flows.append(summary)
    return flows[:100]


async def execute(hass, manager, owner, body, trusted_origin=None):
    action = body.get("action")
    if action == "assistant_status":
        return await get_assistant_status(hass, owner)
    if action == "discover_devices":
        from .discovery import observe

        duration = body.get("duration", 8)
        if type(duration) is not int or not 1 <= duration <= 15:
            raise ValueError("invalid_duration")
        return await observe(hass, manager, duration)
    if action == "xiaomi_setup":
        xiaomi = hass.data[DOMAIN]["xiaomi_setup"]
        if trusted_origin is None:
            setup = await xiaomi.request(owner)
        else:
            setup = await xiaomi.request(owner, trusted_origin)
        telegram = isinstance(trusted_origin, dict) and trusted_origin.get("channel") == "telegram"
        location = (
            "The owner's setup card is available only in the Home Assistant web chat. "
            "For a remaining protected step, ask the user to open that web chat. "
            "If the returned phase is completed, explain that Xiaomi is already connected; do not ask for sign-in again. "
            "Do not claim that a card or password field is displayed in Telegram. "
            if telegram else
            "The owner's setup card is shown inside this Home Assistant web chat. "
        )
        return {"setup": setup,
                "instruction": location + "It installs the pinned official component, asks before restarting, opens Xiaomi sign-in, and imports devices from the selected homes (a single home is automatic). There is no device-picker or password field in the card. Passwords and verification codes are entered only on the official Xiaomi website. A failed local redirect can be pasted only into the card's protected callback field, never chat messages. Explain only controls for the returned phase."}
    if action == "integrations":
        return {"integrations": [{"entry_id": e.entry_id, "domain": e.domain, "title": e.title,
                                  "state": e.state.value} for e in hass.config_entries.async_entries()]}
    if action == "devices":
        devices = device_registry.async_get(hass)
        entities = entity_registry.async_get(hass)
        return {"devices": [{"id": d.id, "name": d.name_by_user or d.name, "model": d.model,
            "manufacturer": d.manufacturer, "area_id": d.area_id,
            "entities": [e.entity_id for e in entity_registry.async_entries_for_device(entities, d.id)]}
            for d in islice(devices.devices, 200)]}
    if action == "discovered_flows":
        return {"flows": discovered_flows(hass, manager)}
    if action == "states":
        states = hass.states.async_all()
        if body.get("entity_id"):
            states = [s for s in states if s.entity_id == body["entity_id"]]
        return {"states": [{"entity_id": s.entity_id, "state": s.state, "attributes": {
            k: v for k, v in s.attributes.items() if k in {"friendly_name", "device_class", "unit_of_measurement",
                "temperature", "current_temperature", "brightness", "supported_features"}}} for s in states[:200]]}
    if action == "todo_items":
        entity_id = body.get("entity_id")
        items = await _todo_items(hass, owner, entity_id)
        return {"entity_id": entity_id, "items": [
            {"summary": item["summary"], "status": item["status"]}
            for item in items[:200]
        ]}
    if action == "services":
        return {"services": {domain: sorted(services) for domain, services in hass.services.async_services().items()}}
    if action == "call_service":
        domain, service = body.get("domain"), body.get("service")
        if not isinstance(domain, str) or not isinstance(service, str) or not hass.services.has_service(domain, service):
            raise ValueError("unknown_service")
        data, target = body.get("data", {}), body.get("target", {})
        if not isinstance(data, dict) or not isinstance(target, dict):
            raise ValueError("invalid_data")
        if domain == "todo" and service == "add_item":
            todo_entity_ids = _todo_entity_ids(target)
            todo_item = data.get("item")
            if not isinstance(todo_item, str) or not todo_item.strip():
                raise ValueError("todo_item_required")
            async with asyncio.timeout(40):
                todo_before = {}
                for entity_id in todo_entity_ids:
                    todo_before[entity_id] = await _todo_items(hass, owner, entity_id)
                async with asyncio.timeout(30):
                    await hass.services.async_call(
                        domain, service, data, target=target, blocking=True,
                        context=Context(user_id=owner),
                    )
                verified_items = []
                for entity_id in todo_entity_ids:
                    after = await _todo_items(hass, owner, entity_id)
                    before_ids = {item["uid"] for item in todo_before[entity_id]
                                  if isinstance(item["uid"], str)}
                    new_matches = [item for item in after
                                   if item["summary"] == todo_item.strip()
                                   and isinstance(item["uid"], str)
                                   and item["uid"] not in before_ids]
                    if len(after) <= len(todo_before[entity_id]) or not new_matches:
                        raise ValueError("todo_add_item_not_verified")
                    verified_items.extend({
                        "entity_id": entity_id,
                        "summary": item["summary"],
                        "status": item["status"],
                    } for item in new_matches)
            return {"called": "todo.add_item", "verification": "persisted",
                    "items": verified_items}
        async with asyncio.timeout(30):
            await hass.services.async_call(domain, service, data, target=target, blocking=True,
                                           context=Context(user_id=owner))
        return {"called": f"{domain}.{service}", "verification": "Read the affected entity with states to verify the result."}
    if action in {"integration_setup_status", "adopt_discovered_flow", "start_integration",
                  "options_start", "continue_integration", "options_continue"}:
        controller = hass.data[DOMAIN]["integration_setup"]
        if trusted_origin is None:
            return await controller.agent_action(owner, action, body)
        return await controller.agent_action(owner, action, body, trusted_origin=trusted_origin)
    raise ValueError("unknown_action")


class SetupActionsView(HomeAssistantView):
    url = "/api/habab_home/setup-actions"
    name = "api:habab_home:setup-actions"
    requires_auth = False  # Local capability, issued only to the admin's child process.

    async def post(self, request):
        hass = request.app["hass"]
        manager = hass.data[DOMAIN]["setup_agent"]
        token = request.headers.get("X-Habab-Agent-Token", "")
        if request.remote not in {"127.0.0.1", "::1"} or not secrets.compare_digest(token, manager.control_token):
            return self.json({"error": "unauthorized"}, status_code=403)
        if manager.active is None:
            return self.json({"error": "inactive"}, status_code=409)
        user = await hass.auth.async_get_user(manager.active[0])
        if user is None or not user.is_active or not user.is_admin:
            return self.json({"error": "unauthorized"}, status_code=403)
        try:
            raw = bytearray()
            async for chunk in request.content.iter_chunked(4096):
                raw.extend(chunk)
                if len(raw) > 16384:
                    return self.json({"error": "too_large"}, status_code=413)
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("invalid")
            assistant = hass.data.get(DOMAIN, {}).get("assistant")
            trusted_origin = (await assistant.trusted_origin(*manager.active)
                              if assistant is not None else None)
            result = await execute(hass, manager, manager.active[0], body, trusted_origin)
            # Config flow result data/defaults are deliberately excluded above.
            return self.json(result)
        except Exception as error:
            _LOGGER.warning("Setup action failed: %s", type(error).__name__)
            return self.json({"error": "action_failed"}, status_code=400)
