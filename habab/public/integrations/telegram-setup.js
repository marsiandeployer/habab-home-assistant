// Register a read-only settings panel in the virtual public Home Assistant demo.
// This code never reads or sends a bot token and never calls a real Telegram API.
(() => {
  if (document.documentElement.dataset.hababDemo !== "true") return;

  const panelPath = "habab-telegram";
  const moduleUrl = "/demos/home-assistant/integrations/telegram-settings.js?v=20261023-1";
  let attempts = 0;
  const timer = setInterval(() => {
    attempts += 1;
    const main = locateHomeAssistantMain(document);
    const hass = main?.hass;
    if (!hass?.updateHass || !hass.panels) {
      if (attempts >= 600) clearInterval(timer);
      return;
    }

    const current = hass.panels[panelPath];
    if (current?.component_name === "custom" &&
        current.config?._panel_custom?.module_url === moduleUrl) {
      clearInterval(timer);
      return;
    }

    hass.updateHass({
      panels: {
        ...hass.panels,
        [panelPath]: {
          component_name: "custom",
          url_path: panelPath,
          title: "Telegram",
          icon: "mdi:message-text",
          config: {
            _panel_custom: {
              name: "habab-telegram-demo-panel",
              module_url: moduleUrl,
              embed_iframe: false,
              trust_external: false,
            },
          },
        },
      },
    });
    clearInterval(timer);
  }, 100);

  function locateHomeAssistantMain(root) {
    for (const element of root.querySelectorAll("*")) {
      if (element.localName === "home-assistant-main") return element;
      if (element.shadowRoot) {
        const found = locateHomeAssistantMain(element.shadowRoot);
        if (found) return found;
      }
    }
    return undefined;
  }
})();
