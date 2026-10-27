"""Secret-free backend facts; client microphone/network observations stay unknown."""
from collections.abc import Mapping
import os

from .const import DOMAIN, VERSION
from .effective_provider import effective_provider_entry
from .gemini_live import DEFAULT_MODEL
from .runtime_context import DOCKER_RUNTIME_KIND, RUNTIME_KIND_ENV
from .voice_context import build_voice_context


def voice_route_metadata(settings):
    """Return the saved provider's default Realtime route without probing it."""
    provider = settings.get("provider") if isinstance(settings, Mapping) else None
    api_key = settings.get("api_key") if isinstance(settings, Mapping) else None
    gemini_live_provider = provider in {"habab", "gemini"}
    has_key = isinstance(api_key, str) and bool(api_key.strip())
    supported = provider == "openai" or (gemini_live_provider and has_key)
    if gemini_live_provider:
        protocol = "gemini_live"
        default_model = DEFAULT_MODEL
    elif provider == "openai":
        protocol = "openai_realtime"
        default_model = "gpt-realtime-mini"
    else:
        protocol = None
        default_model = None
    return {
        "voice_provider": provider if provider in {"habab", "gemini", "openai"} else None,
        "voice_protocol": protocol,
        "default_voice_model": default_model,
        "voice_supported": supported,
        "voice_ready": supported and has_key,
    }


async def get_assistant_status(hass, owner, settings=None, *, verify_telegram=True):
    from .setup_agent import AgentError

    if settings is None:
        try:
            settings = hass.data[DOMAIN]["setup_agent"].settings()
        except AgentError:
            settings = {}
    voice_route = voice_route_metadata(settings)
    config_entries = getattr(hass, "config_entries", None)
    selected_entry = (
        effective_provider_entry(config_entries.async_entries(DOMAIN))
        if config_entries is not None else None
    )
    fast_dialog = (
        selected_entry.options.get("fast_dialog", True) is not False
        if selected_entry is not None else True
    )
    gemini_live_provider = settings.get("provider") in {"habab", "gemini"}
    realtime_supported = voice_route["voice_supported"]
    telegram = hass.data.get("habab_telegram")
    if telegram and verify_telegram:
        telegram_state = await telegram.status(owner)
    elif telegram:
        telegram_state = {"phase": "not_checked", "group": {"phase": "not_checked"}}
    else:
        telegram_state = {}
    home_snapshot = build_voice_context(hass)
    return {
        "assistant": "Habab", "version": VERSION,
        "ha_runtime": os.environ.get(RUNTIME_KIND_ENV, DOCKER_RUNTIME_KIND),
        "web_access": home_snapshot["web_access"],
        "home_snapshot": home_snapshot,
        "selected_provider": settings.get("provider"),
        "selected_text_model": settings.get("model"),
        "fast_dialog": fast_dialog,
        "voice": {
            "realtime_api_registered": True,
            "selected_provider_supports_realtime": realtime_supported,
            "selected_provider_protocol": voice_route["voice_protocol"],
            "required_client_kind": "native_v2" if gemini_live_provider else None,
            "default_voice_model": voice_route["default_voice_model"],
            "supported_clients": {
                "android_with_native_voice": {
                    "wake_detection": "local_vosk", "transport": "native_realtime",
                    "requires_ha_stt_tts": False,
                    "android_local_voice_settings_control_from_chat": False,
                    "custom_sound_selection": False},
                "browser": {"transport": "ha_assist_pipeline", "requires_ha_stt": True},
            },
            "home_assistant_conversation_entity_exposure": {
                "registered": True,
                "scope": "single_exact_entity",
                "runtime_status": "not_checked",
            },
            "observed_client": {"platform": "not_observed", "version": "not_observed",
                                "microphone": "not_observed", "wake_phrase_match": "not_observed",
                                "outbound_audio": "not_observed"},
        },
        "telegram": {
            "phase": telegram_state.get("phase", "not_configured"),
            "group_phase": telegram_state.get("group", {}).get("phase", "not_configured"),
            "settings_path": "/habab-telegram",
        },
    }
