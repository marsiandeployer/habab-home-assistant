"""Narrow OpenCode/Telegram adapter for the native voice-route HA service."""


async def call_select_voice_route(hass, owner, target, context_factory):
    if target not in {"phone", "bluetooth", "toggle"}:
        raise ValueError("invalid_voice_route_target")
    user = await hass.auth.async_get_user(owner) if isinstance(owner, str) else None
    if user is None or not user.is_active or not user.is_admin:
        raise ValueError("not_allowed")
    result = await hass.services.async_call(
        "habab_home", "select_voice_route", {"target": target}, blocking=True,
        context=context_factory(user_id=owner), return_response=True,
    )
    if not isinstance(result, dict) or result.get("status") not in {
            "queued", "applied", "saved", "error"}:
        raise ValueError("invalid_voice_route_response")
    return result


async def call_set_voice_wake(hass, owner, enabled, context_factory):
    if type(enabled) is not bool:
        raise ValueError("invalid_wake_state")
    user = await hass.auth.async_get_user(owner) if isinstance(owner, str) else None
    if user is None or not user.is_active or not user.is_admin:
        raise ValueError("not_allowed")
    result = await hass.services.async_call(
        "habab_home", "set_voice_wake", {"enabled": enabled}, blocking=True,
        context=context_factory(user_id=owner), return_response=True,
    )
    if not isinstance(result, dict) or result.get("status") not in {
            "queued", "applied", "saved", "error"}:
        raise ValueError("invalid_voice_wake_response")
    return result
