"""Authenticated bridge from the owner-scoped HA action to Android voice routing."""

import asyncio

import voluptuous as vol
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, HomeAssistantError, SupportsResponse

from .const import DOMAIN
from .voice_route_state import VoiceRouteMailbox

SERVICE_SELECT_VOICE_ROUTE = "select_voice_route"
SERVICE_SET_VOICE_WAKE = "set_voice_wake"


async def _require_admin(hass, owner):
    if not isinstance(owner, str) or not owner:
        raise HomeAssistantError("not_allowed")
    user = await hass.auth.async_get_user(owner)
    if user is None or not user.is_active or not user.is_admin:
        raise HomeAssistantError("not_allowed")
    return owner


class NativeVoiceRouteView(HomeAssistantView):
    url = "/api/habab_home/native_voice_route"
    name = "api:habab_home:native_voice_route"
    requires_auth = True

    async def post(self, request):
        hass = request.app["hass"]
        user = request.get("hass_user")
        if user is None or not user.is_active or not user.is_admin:
            return self.json_message("not_allowed", status_code=403)
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get("action") != "poll":
                raise ValueError("invalid_voice_route_request")
            result = hass.data[DOMAIN]["voice_route"].poll(
                user.id, body.get("client"), body.get("ack"))
            response = self.json(result)
            response.headers["Cache-Control"] = "no-store"
            return response
        except (ValueError, TypeError):
            return self.json_message("invalid_voice_route_request", status_code=400)


async def async_setup_voice_route(hass: HomeAssistant):
    mailbox = VoiceRouteMailbox()
    hass.data[DOMAIN]["voice_route"] = mailbox
    hass.http.register_view(NativeVoiceRouteView())

    async def enqueue_and_wait(owner, target, queued_message, applied_message, saved_message,
            timeout):
        try:
            command_id, completed = mailbox.enqueue(owner, target)
        except ValueError as error:
            if str(error) == "headset_unavailable":
                return {"status": "error", "requested_target": target,
                        "error": "headset_unavailable",
                        "message": "Bluetooth-гарнитура сейчас недоступна."}
            raise HomeAssistantError(str(error)) from None
        if mailbox.client_active(owner):
            return {"status": "queued", "command_id": command_id,
                    "requested_target": target,
                    "message": queued_message}
        try:
            await asyncio.wait_for(completed.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"status": "queued", "command_id": command_id,
                    "requested_target": target,
                    "message": "Команда ожидает безопасного переключения на Android."}
        result = mailbox.status(owner).get("last_result") or {}
        return {**result, "command_id": command_id, "requested_target": target,
                "message": (saved_message if result.get("status") == "saved"
                            else applied_message if result.get("status") == "applied"
                            else "Команда не выполнена: " + str(result.get("error") or "нет подтверждения маршрута"))}

    async def select_voice_route(call):
        owner = await _require_admin(hass, call.context.user_id)
        target = call.data["target"]
        return await enqueue_and_wait(
            owner, target,
            "Переключу микрофон после сигнала окончания текущего диалога.",
            "Маршрут микрофона переключён и подтверждён Android.",
            "Выбор микрофона сохранён; пробуждение выключено.", 12,
        )

    async def set_voice_wake(call):
        owner = await _require_admin(hass, call.context.user_id)
        if type(call.data["enabled"]) is not bool:
            raise HomeAssistantError("invalid_wake_state")
        target = "wake_on" if call.data["enabled"] else "wake_off"
        return await enqueue_and_wait(
            owner, target,
            "Изменю голосовое ожидание после сигнала окончания текущего диалога.",
            ("Голосовое ожидание включено и подтверждено Android." if call.data["enabled"]
             else "Голосовое ожидание выключено и подтверждено Android."),
            "Голосовое ожидание пока не подтверждено.", 25,
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SELECT_VOICE_ROUTE, select_voice_route,
        schema=vol.Schema({vol.Required("target"): vol.In({"phone", "bluetooth", "toggle"})}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_VOICE_WAKE, set_voice_wake,
        schema=vol.Schema({vol.Required("enabled"): vol.Any(True, False)}),
        supports_response=SupportsResponse.ONLY,
    )
