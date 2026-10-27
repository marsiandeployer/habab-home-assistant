"""Automatic free-beta enrollment using native Home Assistant settings."""

import json
import re
import uuid

import aiohttp

from homeassistant.config_entries import ConfigEntryState, SOURCE_USER
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .effective_provider import effective_provider_entry
from .providers import PROVIDERS


ENROLL_URL = "https://habab-voice.wpmix.net/beta/enroll"
ENROLL_TIMEOUT = 15
MAX_RESPONSE_BYTES = 2048
STORE_KEY = "habab_home.beta"
CODE_PATTERN = re.compile(r"hb1\.([0-9a-f]{32})\.[A-Za-z0-9_-]{43}\Z")
FLOW_ERRORS = {
    "invalid_auth", "invalid_provider", "invalid_model", "quota", "cannot_connect",
    "timeout", "invalid_response", "provider_not_ready", "tools_unsupported",
}


class BetaError(Exception):
    """An enrollment failure safe to return to the administrator."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


async def _installation_id(hass):
    """Persist identity before the first request; never store a credential here."""
    store = Store(hass, 1, STORE_KEY, private=True)
    try:
        stored = await store.async_load()
        if stored is None:
            installation_id = str(uuid.uuid4())
            await store.async_save({"installation_id": installation_id})
            return installation_id
        value = stored.get("installation_id") if isinstance(stored, dict) else None
        parsed = uuid.UUID(value) if isinstance(value, str) else None
        if parsed is None or parsed.version != 4 or str(parsed) != value:
            raise BetaError("habab_beta_storage_failed")
        return value
    except BetaError:
        raise
    except Exception:
        raise BetaError("habab_beta_storage_failed") from None


async def _enroll(session, installation_id):
    try:
        async with session.post(
            ENROLL_URL, json={"installation_id": installation_id},
            timeout=aiohttp.ClientTimeout(total=ENROLL_TIMEOUT), allow_redirects=False,
        ) as response:
            if response.status == 403:
                raise BetaError("habab_beta_issuance_disabled")
            if response.status == 429:
                raise BetaError("habab_beta_rate_limited")
            if response.status != 200:
                raise BetaError("habab_beta_unavailable")
            if (response.content_length is not None
                    and response.content_length > MAX_RESPONSE_BYTES):
                raise BetaError("habab_beta_invalid_response")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(MAX_RESPONSE_BYTES + 1):
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise BetaError("habab_beta_invalid_response")
    except BetaError:
        raise
    except TimeoutError:
        raise BetaError("habab_beta_timeout") from None
    except (aiohttp.ClientError, OSError):
        raise BetaError("habab_beta_cannot_connect") from None
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise BetaError("habab_beta_invalid_response") from None
    code = result.get("code") if isinstance(result, dict) else None
    match = CODE_PATTERN.fullmatch(code) if isinstance(code, str) else None
    if (not isinstance(result, dict) or result.get("installation_id") != installation_id
            or match is None or match[1] != uuid.UUID(installation_id).hex):
        raise BetaError("habab_beta_invalid_response")
    return code


def _entry_status(entry, *, activated):
    return {
        "status": "ready" if activated else "existing",
        "activated": activated,
        "provider": entry.data["provider"],
        "entry_id": entry.entry_id,
        "state": entry.state.value,
    }


async def async_activate(hass):
    """Enroll an unconfigured home without displacing concurrent native setup."""
    async with hass.data[DOMAIN]["settings_lock"]:
        entry = effective_provider_entry(hass.config_entries.async_entries(DOMAIN))
        if entry is not None:
            return _entry_status(entry, activated=False)
        installation_id = await _installation_id(hass)
        code = await _enroll(async_get_clientsession(hass), installation_id)
        # Native HA config flows do not acquire our settings lock. Enrollment
        # waits on the network, so a manual connection may now exist.
        entry = effective_provider_entry(hass.config_entries.async_entries(DOMAIN))
        if entry is not None:
            return _entry_status(entry, activated=False)
        settings = {"provider": "habab", "api_key": code, "model": PROVIDERS["habab"]["model"]}
        try:
            # The normal flow checks the provider before persisting the credential
            # and awaits native entry setup. Do not return its secret-bearing result.
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER, "habab_beta_enrollment": True}, data=settings,
            )
            if result["type"] == "abort" and result.get("reason") == "already_configured":
                manual = effective_provider_entry(hass.config_entries.async_entries(DOMAIN))
                if manual is not None:
                    return _entry_status(manual, activated=False)
                raise BetaError("habab_beta_setup_failed")
            if result["type"] == "form":
                error = result.get("errors", {}).get("base")
                hass.config_entries.flow.async_abort(result["flow_id"])
                raise BetaError(error if error in FLOW_ERRORS else "habab_beta_setup_failed")
            if result["type"] != "create_entry":
                raise BetaError("habab_beta_setup_failed")
            saved = result.get("result")
            current = (
                hass.config_entries.async_get_entry(saved.entry_id)
                if saved is not None else None
            )
            if current is None or current is not saved or current.data != settings:
                raise BetaError("habab_beta_setup_failed")
            manual = effective_provider_entry(
                item for item in hass.config_entries.async_entries(DOMAIN)
                if item.entry_id != current.entry_id
            )
            if manual is not None:
                # Remove only the entry created by this beta flow. Leaving it
                # unselected could still displace a manual Gemini connection
                # through the legacy provider order.
                await hass.config_entries.async_remove(current.entry_id)
                if hass.config_entries.async_get_entry(current.entry_id) is not None:
                    raise BetaError("habab_beta_setup_failed")
                return _entry_status(manual, activated=False)
            if current.state is not ConfigEntryState.LOADED:
                raise BetaError("habab_beta_setup_failed")
            hass.data[DOMAIN]["setup_agent"].select_provider("habab")
            selected = effective_provider_entry(hass.config_entries.async_entries(DOMAIN))
            if selected is not current:
                raise BetaError("habab_beta_setup_failed")
            return _entry_status(current, activated=True)
        except BetaError:
            raise
        except Exception:
            # Native flow and transport exceptions may contain credentials.
            raise BetaError("habab_beta_setup_failed") from None
