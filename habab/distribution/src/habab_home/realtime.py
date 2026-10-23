"""Owner-authenticated ephemeral voice sessions using the shared chat runtime."""
import asyncio
import hashlib
import time

from aiohttp import ClientError, ClientTimeout

from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send, async_dispatcher_connect
from homeassistant.components import websocket_api
from homeassistant.core import callback
import probatio

from .const import DOMAIN
from .setup_agent import AgentError
from .providers import provider_api_base

MODELS = {"gpt-realtime", "gpt-realtime-mini", "gpt-realtime-2.1", "gpt-realtime-2.1-mini"}
INSTRUCTIONS = (
    "Ты голосовой интерфейс домашнего помощника. Пользователь говорит по-русски. "
    "Для каждой реплики вызови ask_home_assistant, передав её смысл без добавления "
    "устройств, действий или условий. Основной помощник хранит общий контекст чата. "
    "После результата инструмента кратко озвучь его ответ по-русски. "
    "Не обещай выполненного действия, если инструмент не подтвердил успех. "
    "Не произноси ключи, токены и технические данные подключения."
)


def session_config(model):
    if model not in MODELS:
        raise ValueError("unsupported_realtime_model")
    return {
        "type": "realtime", "model": model, "instructions": INSTRUCTIONS,
        "output_modalities": ["audio"],
        "audio": {
            "input": {"format": {"type": "audio/pcm", "rate": 24000},
                      "noise_reduction": {"type": "near_field"},
                      "transcription": {"model": "gpt-4o-mini-transcribe", "language": "ru"},
                      "turn_detection": {"type": "server_vad", "threshold": 0.5,
                                         "prefix_padding_ms": 300, "silence_duration_ms": 650,
                                         "create_response": True, "interrupt_response": True}},
            "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "marin"},
        },
        "tools": [{"type": "function", "name": "ask_home_assistant",
                   "description": "Передать реплику общему домашнему помощнику и получить его ответ.",
                   "parameters": {"type": "object", "properties": {
                       "text": {"type": "string", "maxLength": 4000}},
                       "required": ["text"], "additionalProperties": False}}],
        "tool_choice": "required",
    }


def active_admin(request):
    user = request.get("hass_user")
    return user if user and user.is_active and user.is_admin else None


def realtime_base(settings):
    """Use the explicitly selected provider; never borrow another entry's key."""
    if settings.get("provider") not in {"habab", "openai"}:
        raise ValueError("realtime_provider_required")
    return provider_api_base(settings["provider"])


class RealtimeSessionView(HomeAssistantView):
    url = "/api/habab_home/realtime/session"
    name = "api:habab_home:realtime:session"
    requires_auth = True

    def __init__(self):
        self.last_started = {}
        self.lock = asyncio.Lock()

    async def post(self, request):
        owner = active_admin(request)
        if owner is None:
            return self.json_message("not_allowed", status_code=403)
        try:
            data = await request.json()
            config = session_config(data.get("model", "gpt-realtime-mini"))
        except (ValueError, AttributeError):
            return self.json_message("invalid_voice_settings", status_code=400)
        hass = request.app["hass"]
        try:
            settings = hass.data[DOMAIN]["setup_agent"].settings()
            base = realtime_base(settings)
        except AgentError as error:
            return self.json_message(error.code, status_code=400)
        except ValueError:
            return self.json_message("realtime_provider_required", status_code=400)
        async with self.lock:
            now = time.monotonic()
            self.last_started = {key:stamp for key,stamp in self.last_started.items() if now-stamp < 60}
            if now-self.last_started.get(owner.id, -60) < 5:
                return self.json_message("voice_rate_limited", status_code=429)
            self.last_started[owner.id] = now
        try:
            chat = await hass.data[DOMAIN]["assistant"].web_session(owner.id)
            client = async_get_clientsession(hass)
            async with client.post(
                base + "/realtime/client_secrets",
                headers={"Authorization": "Bearer " + settings["api_key"],
                         "OpenAI-Safety-Identifier": hashlib.sha256(owner.id.encode()).hexdigest()},
                json={"expires_after": {"anchor": "created_at", "seconds": 60}, "session": config},
                timeout=ClientTimeout(total=25), allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return self.json_message("realtime_provider_rejected", status_code=502)
                secret = await response.json()
            if not isinstance(secret.get("value"), str) or not secret["value"]:
                return self.json_message("invalid_realtime_response", status_code=502)
        except AgentError as error:
            return self.json_message(error.code, status_code=400)
        except (ClientError, TimeoutError, ValueError):
            return self.json_message("realtime_unavailable", status_code=502)
        result = self.json({"value": secret["value"], "expires_at": secret.get("expires_at"),
                            "session_id": chat["session_id"], "model": config["model"],
                            "websocket_url": "wss://" + base.split("://", 1)[1] + "/realtime?model=" + config["model"]})
        result.headers["Cache-Control"] = "no-store"
        return result


class RealtimeTurnView(HomeAssistantView):
    url = "/api/habab_home/realtime/turn"
    name = "api:habab_home:realtime:turn"
    requires_auth = True

    async def post(self, request):
        owner = active_admin(request)
        if owner is None:
            return self.json_message("not_allowed", status_code=403)
        try:
            data = await request.json()
            session_id, text = data.get("session_id"), data.get("text")
            if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128 or not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000:
                return self.json_message("invalid_voice_turn", status_code=400)
            assistant = request.app["hass"].data[DOMAIN]["assistant"]
            result = await assistant.run(owner.id, "web", session_id, text)
            async_dispatcher_send(request.app["hass"], "habab_voice_history_" + owner.id, result)
        except (ValueError, AttributeError):
            return self.json_message("invalid_voice_turn", status_code=400)
        except AgentError as error:
            return self.json_message(error.code, status_code=400)
        replies = [item["text"] for item in result.get("messages", []) if item.get("role") == "assistant" and item.get("text")]
        if not replies:
            return self.json_message("empty_assistant_reply", status_code=502)
        return self.json({"session_id": session_id, "text": replies[-1][:12000]})


def async_register_views(hass):
    hass.http.register_view(RealtimeSessionView())
    hass.http.register_view(RealtimeTurnView())
    websocket_api.async_register_command(hass, subscribe_history)


@websocket_api.websocket_command({probatio.Required("type"): "habab_home/realtime/subscribe"})
@websocket_api.require_admin
@websocket_api.async_response
async def subscribe_history(hass, connection, msg):
    @callback
    def changed(result):
        if connection.user.is_active and connection.user.is_admin:
            connection.send_event(msg["id"], result)

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(
        hass, "habab_voice_history_" + connection.user.id, changed)
    connection.send_result(msg["id"])
