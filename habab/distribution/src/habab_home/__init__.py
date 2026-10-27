import asyncio
import hashlib
from pathlib import Path

from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.components import frontend
from homeassistant.const import Platform
from homeassistant.const import __version__ as HA_VERSION

from .const import DOMAIN, VERSION


def _assistant_static_paths(frontend_dir: Path, assistant_digest: str):
    assistant_file = frontend_dir / "setup-chat.js"
    assistant_url = f"/habab-home/{assistant_digest}.js"
    return assistant_url, [
        StaticPathConfig(assistant_url, str(assistant_file), False),
        StaticPathConfig("/habab-home", str(frontend_dir), False),
    ]


class HababHomeStatusView(HomeAssistantView):
    url = "/api/habab_home/status"
    name = "api:habab_home:status"
    requires_auth = True

    async def get(self, request):
        hass = request.app["hass"]
        return self.json(
            {
                "product": "Habab Home",
                "version": VERSION,
                "home_assistant_version": HA_VERSION,
                "status": "running",
                "lan_alias": hass.data.get(DOMAIN, {}).get("lan_alias"),
                "capabilities": ["global_chat", "xiaomi_setup", "telegram_agent", "native_discovery", "unified_assistant", "integration_setup"],
            }
        )


async def async_setup(hass, config):
    hass.http.register_view(HababHomeStatusView)
    hass.data[DOMAIN] = {"version": VERSION, "settings_lock": asyncio.Lock()}
    from .mdns import async_setup_alias

    await async_setup_alias(hass)
    frontend_dir = Path(__file__).parent / "frontend"
    assistant_file = frontend_dir / "setup-chat.js"
    assistant_digest = await hass.async_add_executor_job(
        lambda: hashlib.sha256(assistant_file.read_bytes()).hexdigest()[:16]
    )
    assistant_url, static_paths = _assistant_static_paths(frontend_dir, assistant_digest)
    await hass.http.async_register_static_paths(static_paths)
    frontend.add_extra_js_url(hass, assistant_url)
    frontend.add_manifest_json_key("name", "Habab Home Assistant")
    frontend.add_manifest_json_key("short_name", "Habab Home")
    frontend.add_manifest_json_key(
        "icons",
        [
            {
                "src": "/static/habab/habab-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/static/habab/habab-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/static/habab/habab-maskable-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    )
    from .setup_agent import SetupAgent

    agent = SetupAgent(hass)
    await agent.setup()
    hass.data[DOMAIN]["setup_agent"] = agent
    from .assistant import AssistantService

    assistant = AssistantService(hass, agent)
    await assistant.setup()
    hass.data[DOMAIN]["assistant"] = assistant
    from .realtime import async_register_views as register_realtime_views

    register_realtime_views(hass)
    from .diagnostics import async_register_views as register_diagnostics_views

    register_diagnostics_views(hass)
    from .feedback import async_register_views as register_feedback_views

    register_feedback_views(hass)
    from .integration_setup import IntegrationSetup, async_register_commands as register_integration_setup_commands

    hass.data[DOMAIN]["integration_setup"] = IntegrationSetup(hass, agent)
    register_integration_setup_commands(hass)
    from .automation_setup import AutomationSetup, async_register_commands as register_automation_setup_commands

    hass.data[DOMAIN]["automation_setup"] = AutomationSetup(hass)
    await hass.data[DOMAIN]["automation_setup"].setup()
    register_automation_setup_commands(hass)
    from .telemetry import TelemetryManager, async_register_commands as async_register_telemetry_commands

    telemetry = TelemetryManager(hass)
    await telemetry.async_setup()
    hass.data[DOMAIN]["telemetry"] = telemetry
    async_register_telemetry_commands(hass)
    from .xiaomi_setup import XiaomiSetup, async_register_commands as register_xiaomi_commands

    xiaomi_setup = XiaomiSetup(hass)
    await xiaomi_setup.setup()
    hass.data[DOMAIN]["xiaomi_setup"] = xiaomi_setup
    register_xiaomi_commands(hass)
    from .discovery import async_register_commands as register_discovery_commands

    register_discovery_commands(hass)
    from .setup_actions import SetupActionsView

    hass.http.register_view(SetupActionsView)
    await async_register_panel(
        hass, frontend_url_path="habab-ai", webcomponent_name="habab-ai-panel",
        sidebar_title="AI", sidebar_icon="mdi:assistant", require_admin=True,
        module_url="/habab-home/ai-panel.js?v=4",
    )
    await async_register_panel(
        hass, frontend_url_path="habab-telegram", webcomponent_name="habab-telegram-panel",
        sidebar_title="Telegram", sidebar_icon="mdi:message-text", require_admin=True,
        module_url="/habab-home/telegram-panel.js?v=1",
    )
    await async_register_panel(
        hass, frontend_url_path="habab-diagnostics", webcomponent_name="habab-diagnostics-panel",
        sidebar_title="История и диагностика", sidebar_icon="mdi:clipboard-text-clock",
        require_admin=True, module_url="/habab-home/diagnostics-panel.js?v=1",
    )
    await async_register_panel(
        hass, frontend_url_path="habab-support", webcomponent_name="habab-support-panel",
        sidebar_title="Поддержка (бета)", sidebar_icon="mdi:message-question-outline",
        require_admin=False, module_url="/habab-home/support-panel.js?v=1",
    )
    from .websocket import async_register_commands

    async_register_commands(hass)
    return True


async def async_setup_entry(hass, entry):
    platforms = [Platform.CONVERSATION]
    if entry.data["provider"] == "openai":
        platforms.extend([Platform.STT, Platform.TTS])
    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    return True


async def async_unload_entry(hass, entry):
    # Also remove speech platforms left by a legacy Habab entry on reload.
    platforms = [Platform.CONVERSATION]
    if entry.data["provider"] in {"openai", "habab"}:
        platforms.extend([Platform.STT, Platform.TTS])
    return await hass.config_entries.async_unload_platforms(entry, platforms)
