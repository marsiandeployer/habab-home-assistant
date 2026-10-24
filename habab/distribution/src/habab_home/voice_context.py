"""Small, read-only snapshot for conversational voice bootstrap."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from homeassistant.components.homeassistant import exposed_entities
    from homeassistant.helpers import area_registry, device_registry, entity_registry
except ImportError:  # Fail closed when running against an unsupported HA package.
    exposed_entities = None
    area_registry = None
    device_registry = None
    entity_registry = None


_MAX_AGE_SECONDS = 30
_MAX_BYTES = 4800
_MAX_DEVICE_STATES = 12
_MAX_WEATHER_STATES = 2
_DEVICE_DOMAINS = {"light", "switch", "climate", "vacuum"}
_WEATHER_NUMERIC_BOUNDS = {
    "temperature": (-100.0, 100.0),
    "apparent_temperature": (-100.0, 100.0),
    "dew_point": (-100.0, 100.0),
    "humidity": (0.0, 100.0),
    "pressure": (100.0, 1200.0),
    "wind_speed": (0.0, 150.0),
    "visibility": (0.0, 1000.0),
}
_DEVICE_NUMERIC_BOUNDS = {
    "brightness": (0.0, 255.0),
    "temperature": (-100.0, 100.0),
    "current_temperature": (-100.0, 100.0),
    "humidity": (0.0, 100.0),
    "battery_level": (0.0, 100.0),
}
_SAFE_UNITS = {
    "°C", "°F", "K", "%", "hPa", "mbar", "Pa", "km/h", "m/s",
    "mph", "kn", "kt", "km", "mi", "m", "ft",
}


def _bounded_text(value, limit: int) -> str:
    if not isinstance(value, str):
        return "unknown"
    value = "".join(character for character in value if ord(character) >= 32 and ord(character) != 127)
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _clock(hass, now):
    configured_zone = getattr(getattr(hass, "config", None), "time_zone", None)
    try:
        zone = ZoneInfo(configured_zone) if isinstance(configured_zone, str) else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None

    if zone is None:
        clock_zone = timezone.utc
        zone_name = "unknown"
    else:
        clock_zone = zone
        zone_name = _bounded_text(configured_zone, 80)

    current = now if isinstance(now, datetime) else datetime.now(clock_zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=clock_zone)
    else:
        current = current.astimezone(clock_zone)

    generated_at = current.isoformat(timespec="seconds")
    expires_at = (current.timestamp() + _MAX_AGE_SECONDS)
    expires = datetime.fromtimestamp(expires_at, clock_zone).isoformat(timespec="seconds")
    return {
        "generated_at": generated_at,
        "time_zone": zone_name,
        "local_time": current.strftime("%Y-%m-%d %H:%M:%S") if zone else "unknown",
        "expires_at": expires,
    }


def _unknown_context(clock):
    return {
        **clock,
        "snapshot_kind": "observed_home_state",
        "max_age_seconds": _MAX_AGE_SECONDS,
        "assist_exposure": "unknown",
        "weather": {"status": "unknown", "entities": []},
        "states": [],
        "truncated": False,
    }


def _numeric_attributes(state, bounds):
    row = {}
    attributes = getattr(state, "attributes", {})
    if not isinstance(attributes, Mapping):
        return row
    for key, (minimum, maximum) in bounds.items():
        value = attributes.get(key)
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and minimum <= value <= maximum and math.isfinite(value)):
            row[key] = value
    for key in ("temperature_unit", "unit_of_measurement", "wind_speed_unit", "pressure_unit"):
        value = attributes.get(key)
        if isinstance(value, str) and value in _SAFE_UNITS:
            row[key] = value
    return row


def _entity_row(hass, state, entity_registry_obj, device_registry_obj, area_registry_obj, bounds):
    entity_id = state.entity_id
    registered = entity_registry_obj.async_get(entity_id)
    if registered is not None and (
        getattr(registered, "disabled_by", None) is not None
        or getattr(registered, "hidden_by", None) is not None
    ):
        return None

    row = {
        "entity_id": _bounded_text(entity_id, 120),
        "name": _bounded_text(getattr(state, "name", entity_id), 80),
        "state": _bounded_text(getattr(state, "state", "unknown"), 64),
    }
    row.update(_numeric_attributes(state, bounds))

    area_id = getattr(registered, "area_id", None) if registered else None
    device_id = getattr(registered, "device_id", None) if registered else None
    if not area_id and device_id:
        device = device_registry_obj.async_get(device_id)
        area_id = getattr(device, "area_id", None) if device else None
    if area_id:
        area = area_registry_obj.async_get_area(area_id)
        if area is not None:
            row["room"] = _bounded_text(area.name, 80)
    return row


def build_voice_context(hass, *, now=None) -> dict:
    """Return a bounded snapshot of HA time, exposed weather, and device states.

    This function only reads in-memory HA state and registries. If the current HA
    Assist exposure contract cannot be checked, it returns an explicit unknown
    snapshot with no entity data.
    """
    clock = _clock(hass, now)
    context = _unknown_context(clock)

    should_expose = getattr(exposed_entities, "async_should_expose", None)
    if not callable(should_expose) or any(
        registry_module is None
        for registry_module in (entity_registry, device_registry, area_registry)
    ):
        return context

    try:
        entity_registry_obj = entity_registry.async_get(hass)
        device_registry_obj = device_registry.async_get(hass)
        area_registry_obj = area_registry.async_get(hass)
        all_states = hass.states.async_all()
    except Exception:
        return context

    states = []
    weather = []
    truncated = False
    try:
        ordered = sorted(all_states, key=lambda item: item.entity_id)
        for state in ordered:
            domain = getattr(state, "domain", None)
            if domain not in _DEVICE_DOMAINS and domain != "weather":
                continue
            exposed = should_expose(hass, "conversation", state.entity_id)
            if not isinstance(exposed, bool):
                raise RuntimeError("unsupported Assist exposure result")
            if not exposed:
                continue
            limits = _WEATHER_NUMERIC_BOUNDS if domain == "weather" else _DEVICE_NUMERIC_BOUNDS
            row = _entity_row(
                hass, state, entity_registry_obj, device_registry_obj, area_registry_obj, limits
            )
            if row is None:
                continue

            target = weather if domain == "weather" else states
            limit = _MAX_WEATHER_STATES if domain == "weather" else _MAX_DEVICE_STATES
            if len(target) >= limit:
                truncated = True
                continue
            target.append(row)
    except Exception:
        # A partial snapshot would be hard to interpret and may bypass an ACL
        # failure partway through enumeration. Discard all entity data instead.
        return context

    context.update({
        "assist_exposure": "verified",
        "weather": {
            "status": "available" if weather else "unavailable",
            "entities": weather,
        },
        "states": states,
        "truncated": truncated,
    })

    while len(json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > _MAX_BYTES:
        if states:
            states.pop()
        elif weather:
            weather.pop()
            context["weather"]["status"] = "available" if weather else "unavailable"
        else:
            # Fixed fields alone are tiny; this is a final defensive fallback.
            return _unknown_context(clock)
        context["truncated"] = True
    return context
