"""Local capability bridge for an already authorized administrator's OpenCode turn."""
import asyncio
from itertools import islice
import json
import logging
import re
import secrets

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import DISCOVERY_SOURCES, SOURCE_USER
from homeassistant.core import Context
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.selector import SelectSelector

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def flow_result(result):
    """Expose next field names, never config-entry data/defaults or credentials."""
    schema = getattr(result.get("data_schema"), "schema", {})
    fields = []
    if isinstance(schema, dict):
        for key, validator in schema.items():
            field_type = getattr(validator, "selector_type", None)
            if not isinstance(field_type, str):
                validator_type = validator if isinstance(validator, type) else type(validator)
                field_type = validator_type.__name__.lower()
            field = {
                "name": str(getattr(key, "schema", key)),
                "type": field_type,
            }
            if isinstance(validator, SelectSelector):
                options = validator.config.get("options", [])
                field["options"] = [
                    option if isinstance(option, str) else {
                        "value": option["value"], "label": option.get("label", option["value"]),
                    }
                    for option in options
                    if isinstance(option, str) or (
                        isinstance(option, dict)
                        and isinstance(option.get("value"), str)
                        and isinstance(option.get("label", option["value"]), str)
                    )
                ]
            fields.append(field)
    return {key: result[key] for key in ("type", "flow_id", "step_id", "errors", "menu_options") if key in result} | {"fields": fields}


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


async def execute(hass, manager, owner, body):
    action = body.get("action")
    if action == "discover_devices":
        from .discovery import observe

        duration = body.get("duration", 8)
        if type(duration) is not int or not 1 <= duration <= 15:
            raise ValueError("invalid_duration")
        return await observe(hass, manager, duration)
    if action == "xiaomi_setup":
        return {"setup": await hass.data[DOMAIN]["xiaomi_setup"].request(owner),
                "instruction": "The owner's setup card is shown inside this Home Assistant chat. It installs the pinned official component, asks before restarting, opens Xiaomi sign-in, and imports devices from the selected homes (a single home is automatic). There is no device-picker or password field in the card. Passwords and verification codes are entered only on the official Xiaomi website. A failed local redirect can be pasted only into the card's protected callback field, never chat messages. Explain only controls for the returned phase."}
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
    if action == "services":
        return {"services": {domain: sorted(services) for domain, services in hass.services.async_services().items()}}
    if action == "call_service":
        domain, service = body.get("domain"), body.get("service")
        if not isinstance(domain, str) or not isinstance(service, str) or not hass.services.has_service(domain, service):
            raise ValueError("unknown_service")
        data, target = body.get("data", {}), body.get("target", {})
        if not isinstance(data, dict) or not isinstance(target, dict):
            raise ValueError("invalid_data")
        async with asyncio.timeout(30):
            await hass.services.async_call(domain, service, data, target=target, blocking=True,
                                           context=Context(user_id=owner))
        return {"called": f"{domain}.{service}", "verification": "Read the affected entity with states to verify the result."}
    if action == "adopt_discovered_flow":
        flow_id = body.get("flow_id")
        if not isinstance(flow_id, str) or flow_id in manager.flow_owners:
            raise ValueError("unknown_flow")
        flow = next((item for item in hass.config_entries.flow.async_progress()
                     if item.get("flow_id") == flow_id), None)
        context = flow.get("context") if flow else None
        if not isinstance(context, dict) or context.get("source") not in DISCOVERY_SOURCES:
            raise ValueError("unknown_flow")
        manager.flow_owners[flow_id] = (owner, "config")
        return flow_result(flow)
    if action in {"start_integration", "options_start"}:
        if action == "start_integration":
            domain = body.get("domain", "")
            if not isinstance(domain, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", domain):
                raise ValueError("invalid_domain")
            result = await hass.config_entries.flow.async_init(domain, context={"source": SOURCE_USER})
            kind = "config"
        else:
            entry_id = body.get("entry_id")
            if not isinstance(entry_id, str) or not hass.config_entries.async_get_entry(entry_id):
                raise ValueError("unknown_entry")
            result = await hass.config_entries.options.async_init(entry_id)
            kind = "options"
        if result.get("flow_id"):
            manager.flow_owners[result["flow_id"]] = (owner, kind)
        return flow_result(result)
    if action in {"continue_integration", "options_continue"}:
        flow_id, data = body.get("flow_id"), body.get("input", {})
        kind = "options" if action == "options_continue" else "config"
        if not isinstance(flow_id, str) or manager.flow_owners.get(flow_id) != (owner, kind):
            raise ValueError("unknown_flow")
        if not isinstance(data, dict):
            raise ValueError("invalid_input")
        flows = hass.config_entries.options if kind == "options" else hass.config_entries.flow
        result = await flows.async_configure(flow_id, data)
        if result["type"] in {"create_entry", "abort"}:
            manager.flow_owners.pop(flow_id, None)
        return flow_result(result)
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
            result = await execute(hass, manager, manager.active[0], body)
            # Config flow result data/defaults are deliberately excluded above.
            return self.json(result)
        except Exception as error:
            _LOGGER.warning("Setup action failed: %s", type(error).__name__)
            return self.json({"error": "action_failed"}, status_code=400)
