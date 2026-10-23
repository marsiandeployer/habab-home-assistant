"""Private Telegram bot credentials are validated and stored by HA."""
import re

import aiohttp

import probatio

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .transport import get_me_request, proxy_url


async def get_bot(session, token, proxy=None):
    """Validate a token without exposing Telegram's response or URL."""
    if not isinstance(token, str) or not re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,256}", token):
        raise ValueError("invalid_token")
    proxy = proxy_url(proxy)
    try:
        url, headers = get_me_request(token, proxy)
        async with session.get(
            url, headers=headers,
            **({"proxy": proxy} if proxy else {}),
            timeout=aiohttp.ClientTimeout(total=10), allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise ValueError("invalid_token" if response.status in (401, 404) else "cannot_validate")
            payload = await response.json(content_type=None)
            bot = payload.get("result", {}) if payload.get("ok") is True else {}
            if not isinstance(bot.get("id"), int) or not isinstance(bot.get("username"), str):
                raise ValueError("invalid_token")
            return {"id": bot["id"], "username": bot["username"]}
    except ValueError as err:
        if str(err) in ("invalid_token", "cannot_validate"):
            raise
        raise ValueError("cannot_validate") from None
    except Exception:
        raise ValueError("cannot_validate") from None


class HababTelegramConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        owner_id = self.context.get("habab_owner_id")
        if not isinstance(owner_id, str) or not owner_id:
            return self.async_abort(reason="not_allowed")
        user = await self.hass.auth.async_get_user(owner_id)
        if user is None or not user.is_active or not user.is_admin:
            return self.async_abort(reason="not_allowed")
        errors = {}
        if user_input:
            token = user_input.get("token", "").strip()
            if not token or len(token) > 256:
                errors["base"] = "invalid_token"
            else:
                try:
                    proxy = proxy_url(user_input.get("proxy_url", "").strip())
                    bot = await get_bot(async_get_clientsession(self.hass), token, proxy)
                except ValueError as err:
                    errors["base"] = str(err)
                else:
                    await self.async_set_unique_id(owner_id)
                    current = next((entry for entry in self._async_current_entries()
                                    if entry.unique_id == owner_id), None)
                    data = {"token": token, "owner_id": owner_id,
                            "bot_id": bot["id"], "bot_username": bot["username"]}
                    if proxy:
                        data["proxy_url"] = proxy
                    if current:
                        self.hass.config_entries.async_update_entry(current, data=data)
                        if not await self.hass.config_entries.async_reload(current.entry_id):
                            return self.async_abort(reason="setup_failed")
                        return self.async_abort(reason="reconfigured")
                    return self.async_create_entry(title=f"@{bot['username']}", data=data)

        return self.async_show_form(
            step_id="user", data_schema=probatio.Schema({
                probatio.Required("token"): probatio.All(
                    str, probatio.Length(min=1, max=256),
                ),
                probatio.Optional("proxy_url"): probatio.All(
                    str, probatio.Length(max=2048),
                ),
            }), errors=errors,
        )
