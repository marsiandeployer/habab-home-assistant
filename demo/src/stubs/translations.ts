import type { LocalizeFunc } from "../../../src/common/translations/localize";
import type { MockHomeAssistant } from "../../../src/fake_data/provide_hass";
import { connectivityBackendTranslations } from "./connectivity/fixtures";

// Native component-state strings are supplied by Home Assistant Core in a
// real installation. The standalone demo has no Core, so seed the same keys
// from the official Russian locale artifact used by this build.
const demoComponentTranslations = {
  "component.light.entity_component._.state.on": "Включено",
  "component.light.entity_component._.state.off": "Выключено",
  "component.lock.entity_component._.state.locked": "Заблокировано",
  "component.lock.entity_component._.state.unlocked": "Открыто",
  "component.climate.entity_component._.state.heat": "Обогрев",
  "component.cover.entity_component._.state.open": "Открыто",
  "component.cover.entity_component._.state.closed": "Закрыто",
  "component.weather.entity_component._.state.partlycloudy":
    "Переменная облачность",
};

export const mockTranslations = (
  hass: MockHomeAssistant,
  localizePromise?: Promise<LocalizeFunc>
) => {
  hass.mockWS(
    "frontend/get_translations",
    (msg: { language: string; category?: string }) => ({
      resources: {
        ...(msg.category && connectivityBackendTranslations[msg.category]),
        ...(msg.category === "entity_component" ? demoComponentTranslations : {}),
      },
    })
  );

  // `hass.loadBackendTranslation` is a no-op in the mocked hass, so categories
  // that are only requested through it never reach the WebSocket mock above.
  // Seed every category into the resources, after the fragment translations so
  // this merges on top of them.
  (localizePromise ?? Promise.resolve()).then(() =>
    hass.addTranslations(
      Object.assign(
        {},
        ...Object.values(connectivityBackendTranslations),
        demoComponentTranslations
      )
    )
  );
};
