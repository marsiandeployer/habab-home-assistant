from homeassistant.components.http import HomeAssistantView
from homeassistant.const import __version__ as HA_VERSION

from .const import DOMAIN, VERSION


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
            }
        )


async def async_setup(hass, config):
    hass.http.register_view(HababHomeStatusView)
    hass.data[DOMAIN] = {"version": VERSION}
    return True
