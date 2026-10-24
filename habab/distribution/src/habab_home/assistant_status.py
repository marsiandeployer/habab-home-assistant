"""Secret-free backend facts; client microphone/network observations stay unknown."""
import os

from .const import DOMAIN, VERSION
from .runtime_context import DOCKER_RUNTIME_KIND, RUNTIME_KIND_ENV


async def get_assistant_status(hass, owner, settings=None, *, verify_telegram=True):
    from .realtime import realtime_base
    from .gemini_live import DEFAULT_MODEL
    from .setup_agent import AgentError

    if settings is None:
        try:
            settings = hass.data[DOMAIN]["setup_agent"].settings()
        except AgentError:
            settings = {}
    try:
        if settings.get("provider") == "gemini":
            if not settings.get("api_key"):
                raise ValueError("realtime_provider_required")
        else:
            realtime_base(settings)
        realtime_supported = True
    except ValueError:
        realtime_supported = False
    telegram = hass.data.get("habab_telegram")
    if telegram and verify_telegram:
        telegram_state = await telegram.status(owner)
    elif telegram:
        telegram_state = {"phase": "not_checked", "group": {"phase": "not_checked"}}
    else:
        telegram_state = {}
    return {
        "assistant": "Habab", "version": VERSION,
        "ha_runtime": os.environ.get(RUNTIME_KIND_ENV, DOCKER_RUNTIME_KIND),
        "selected_provider": settings.get("provider"),
        "selected_text_model": settings.get("model"),
        "voice": {
            "realtime_api_registered": True,
            "selected_provider_supports_realtime": realtime_supported,
            "selected_provider_protocol": "gemini_live" if settings.get("provider") == "gemini" else "openai_realtime" if realtime_supported else None,
            "required_client_kind": "native_v2" if settings.get("provider") == "gemini" else None,
            "default_voice_model": DEFAULT_MODEL if settings.get("provider") == "gemini" else "gpt-realtime-mini" if realtime_supported else None,
            "supported_clients": {
                "android_with_native_voice": {
                    "wake_detection": "local_vosk", "transport": "native_realtime",
                    "requires_ha_stt_tts": False, "settings_control_from_chat": False,
                    "custom_sound_selection": False},
                "browser": {"transport": "ha_assist_pipeline", "requires_ha_stt": True},
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
