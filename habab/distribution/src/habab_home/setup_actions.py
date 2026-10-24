"""Local capability bridge for an already authorized administrator's OpenCode turn."""
import asyncio
from itertools import islice
import json
import logging
import os
import secrets

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import DISCOVERY_SOURCES
from homeassistant.core import Context
from homeassistant.helpers import device_registry, entity_registry
from .const import DOMAIN, VERSION

_LOGGER = logging.getLogger(__name__)


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
        from .realtime import realtime_base
        from .setup_agent import AgentError
        from .runtime_context import DOCKER_RUNTIME_KIND, RUNTIME_KIND_ENV

        try:
            settings = hass.data[DOMAIN]["setup_agent"].settings()
        except AgentError:
            settings = {}
        try:
            realtime_base(settings)
            realtime_supported = True
        except ValueError:
            realtime_supported = False
        telegram = hass.data.get("habab_telegram")
        telegram_state = await telegram.status(owner) if telegram else {}
        return {"assistant": "Habab", "version": VERSION,
                "ha_runtime": os.environ.get(RUNTIME_KIND_ENV, DOCKER_RUNTIME_KIND),
                "selected_provider": settings.get("provider"),
                "voice": {
                    "realtime_api_registered": True,
                    "selected_provider_supports_realtime": realtime_supported,
                    "android": {"wake_detection": "local_vosk", "transport": "native_realtime",
                                "requires_ha_stt_tts": False},
                    "browser": {"transport": "ha_assist_pipeline", "requires_ha_stt": True},
                    "client_microphone_and_network": "not_observed"},
                "telegram": {"phase": telegram_state.get("phase", "not_configured"),
                             "group_phase": telegram_state.get("group", {}).get("phase", "not_configured"),
                             "settings_path": "/habab-telegram"}}
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
        return {"setup": setup,
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
