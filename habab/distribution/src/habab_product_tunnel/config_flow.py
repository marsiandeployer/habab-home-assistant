"""Native Home Assistant setup for the per-home remote tunnel."""

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import DOMAIN


def _schema(defaults=None, *, existing_token=False):
    defaults = defaults or {}
    schema = {
        vol.Required("relay", default=defaults.get("relay", "")): str,
        vol.Required("home", default=defaults.get("home", "")): str,
    }
    token_selector = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
    schema[(vol.Optional if existing_token else vol.Required)("token")] = token_selector
    return vol.Schema(schema)


class HababTunnelConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HababTunnelOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                # This is the canonical tunnel module staged into the Docker image.
                from .web_tunnel import hostname, validate_relay

                home = hostname(user_input["home"])
                validate_relay(user_input["relay"])
                token = user_input["token"]
                if (len(token) < 32 or not token.isascii()
                        or any(char.isspace() for char in token)):
                    raise ValueError("invalid home token")
                await self.async_set_unique_id(home)
                self._abort_if_unique_id_configured()
            except (KeyError, TypeError, ValueError):
                errors["base"] = "invalid_config"
            else:
                return self.async_create_entry(
                    title=f"Удалённый доступ · {home}",
                    data={"relay": user_input["relay"], "home": home, "token": token},
                )
        return self.async_show_form(step_id="user", data_schema=_schema(), errors=errors)


class HababTunnelOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                from .web_tunnel import hostname, validate_relay

                home = hostname(user_input["home"])
                if home != self.config_entry.data["home"]:
                    raise ValueError("home identity cannot be changed in options")
                validate_relay(user_input["relay"])
                token = (user_input.get("token") or self.config_entry.options.get("token")
                         or self.config_entry.data.get("token", ""))
                if (len(token) < 32 or not token.isascii()
                        or any(char.isspace() for char in token)):
                    raise ValueError("invalid home token")
            except (KeyError, TypeError, ValueError):
                errors["base"] = "invalid_config"
            else:
                return self.async_create_entry(
                    title="",
                    data={"relay": user_input["relay"], "home": home, "token": token},
                )
        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=_schema(defaults, existing_token=True), errors=errors,
        )
