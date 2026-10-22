"""Local capability bridge for an already authorized administrator's OpenCode turn."""
import asyncio
import json
import logging
import re
import secrets

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import Context
from homeassistant.helpers import device_registry, entity_registry

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def flow_result(result):
    """Expose next field names, never config-entry data/defaults or credentials."""
    schema = getattr(result.get("data_schema"), "schema", {})
    fields = [str(getattr(key, "schema", key)) for key in schema] if isinstance(schema, dict) else []
    return {key: result[key] for key in ("type", "flow_id", "step_id", "errors", "menu_options") if key in result} | {"fields": fields}


async def execute(hass, manager, owner, body):
    action = body.get("action")
    if action == "integrations":
        return {"integrations": [{"entry_id": e.entry_id, "domain": e.domain, "title": e.title,
                                  "state": e.state.value} for e in hass.config_entries.async_entries()]}
    if action == "devices":
        devices = device_registry.async_get(hass)
        entities = entity_registry.async_get(hass)
        return {"devices": [{"id": d.id, "name": d.name_by_user or d.name, "model": d.model,
            "manufacturer": d.manufacturer, "area_id": d.area_id,
            "entities": [e.entity_id for e in entity_registry.async_entries_for_device(entities, d.id)]}
            for d in list(devices.devices.values())[:200]]}
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
