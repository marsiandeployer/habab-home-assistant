import { customElement } from "lit/decorators";
import type { LocalizeFunc } from "../../src/common/translations/localize";
import { isNavigationClick } from "../../src/common/dom/is-navigation-click";
import { navigate } from "../../src/common/navigate";
import type { MockHomeAssistant } from "../../src/fake_data/provide_hass";
import { provideHass } from "../../src/fake_data/provide_hass";
import { HomeAssistantAppEl } from "../../src/layouts/home-assistant";
import type { HomeAssistant } from "../../src/types";
import {
  applyDemoTheme,
  demoConfigEntities,
  registerDemoAutomations,
  selectedDemo,
  selectedDemoConfig,
} from "./configs/demo-configs";
import { mockAreaRegistry, setDemoAreas } from "./stubs/area_registry";
import {
  connectivityCommands,
  connectivityComponents,
  connectivityEntities,
  connectivityEntityRegistryEntries,
} from "./stubs/connectivity/fixtures";
import { mockAuth } from "./stubs/auth";
import { demoDevices } from "./stubs/devices";
import { mockDeviceRegistry } from "./stubs/device_registry";
import { mockEnergy } from "./stubs/energy";
import { energyEntities } from "./stubs/entities";
import {
  mockEntityRegistry,
  mockEntityRegistryDisplay,
} from "./stubs/entity_registry";
import { mockEvents } from "./stubs/events";
import { mockFloorRegistry, setDemoFloors } from "./stubs/floor_registry";
import { mockFrontend } from "./stubs/frontend";
import { mockHardware } from "./stubs/hardware";
import { mockHassioSupervisor } from "./stubs/hassio_supervisor";
import { mockIntegration } from "./stubs/integration";
import { mockLabelRegistry } from "./stubs/label_registry";
import { automationPlatformDomains } from "./stubs/automation_platform_domains";
import { mockIcons } from "./stubs/icons";
import { mockHistory } from "./stubs/history";
import { mockLovelace } from "./stubs/lovelace";
import { zoneRegistryEntries } from "./stubs/map";
import { mockMediaPlayer } from "./stubs/media_player";
import { mockPersistentNotification } from "./stubs/persistent_notification";
import { mockRecorder } from "./stubs/recorder";
import { mockSensor } from "./stubs/sensor";
import { mockSystemLog } from "./stubs/system_log";
import { mockTemplate } from "./stubs/template";
import { mockTodo } from "./stubs/todo";
import { mockTranslations } from "./stubs/translations";
import { mockUsagePrediction } from "./stubs/usage_prediction";

// WS command / REST path prefixes whose mocks live in the lazily imported
// config-panel chunk (see ./stubs/config-panel). Must stay in sync with it.
const CONFIG_PANEL_COMMANDS = [
  "cloud/",
  "webhook/list",
  "validate_config",
  "config_entries/",
  "device_automation/",
  "entity/source",
  "blueprint/",
  "homeassistant/expose",
  "zone/list",
  "person/list",
  "network/url",
  "application_credentials/",
  "system_health/",
  "backup/",
  "automation/config",
  "trigger_platforms/subscribe",
  "condition_platforms/subscribe",
  "get_triggers_for_target",
  "get_conditions_for_target",
  "get_services_for_target",
  "extract_from_target",
  "script/config",
  "config/automation/config",
  "config/script/config",
  "config/scene/config",
  "search/related",
  "tag/list",
  "assist_pipeline/",
  "config/entity_registry/settings/",
  "slugify",
  "marketplace/",
  ...connectivityCommands,
];

@customElement("ha-demo")
export class HaDemo extends HomeAssistantAppEl {
  protected async _initializeHass() {
    let demoStateReady = false;
    const installedStateHooks = new WeakSet<object>();
    const lastPersistedState: Record<string, string> = {};

    const installDemoStateHooks = () => {
      for (const [entityId, entity] of Object.entries(hass.mockEntities) as any) {
        if (!isDemoPersistedEntity(entityId) || !entity?.hass) continue;
        const entityHass = entity.hass as MockHomeAssistant;
        if (installedStateHooks.has(entityHass)) continue;
        installedStateHooks.add(entityHass);
        const updateStates = entityHass.updateStates.bind(entityHass);
        entityHass.updateStates = (updates) => {
          const changed: string[] = [];
          if (demoStateReady) {
            for (const [updatedId, next] of Object.entries(updates)) {
              if (!isDemoPersistedEntity(updatedId)) continue;
              const value = JSON.stringify({
                state: next.state,
                brightness: next.attributes.brightness,
                temperature: next.attributes.temperature,
              });
              if (lastPersistedState[updatedId] !== value) {
                lastPersistedState[updatedId] = value;
                changed.push(updatedId);
              }
            }
          }
          const result = updateStates(updates);
          if (changed.length) {
            void persistDemoEntities(this.hass!, changed).catch(() => undefined);
          }
          return result;
        };
      }
    };

    const initial: Partial<MockHomeAssistant> = {
      panelUrl: (this as any)._panelUrl,
      // Override updateHass so that the correct hass lifecycle methods are called
      updateHass: (hassUpdate: Partial<HomeAssistant>) =>
        this._updateHass(hassUpdate),
    };

    // `false` for contexts: HomeAssistantAppEl already provides them via
    // `contextMixin`, so let provideHass skip them to avoid duplicate providers.
    const hass = provideHass(this, initial, true, false);
    hass.updateHass({
      language: "ru",
      selectedLanguage: "ru",
      locale: { ...hass.locale, language: "ru" },
    });

    hass.updateHass({
      config: {
        ...hass.config,
        components: [
          // Some automation platforms are loaded already
          ...new Set([
            ...(hass.config?.components ?? []),
            "backup",
            "webhook",
            "usage_prediction",
            "assist_pipeline",
            "hassio",
            "hardware",
            "marketplace",
            ...connectivityComponents,
            ...automationPlatformDomains,
          ]),
        ],
      },
      panels: {
        ...hass.panels,
        // Opened from the settings page, not listed in the sidebar
        marketplace: {
          component_name: "marketplace",
          icon: null,
          title: null,
          config: null,
          url_path: "marketplace",
        },
      },
    });

    const localizePromise =
      // @ts-ignore
      this._loadFragmentTranslations(hass.language, "page-demo").then(
        // hass is not updated yet when another fragment started loading
        // meanwhile, so use the localize with these translations directly
        (localize?: LocalizeFunc) => localize ?? this.hass!.localize
      );

    mockLovelace(hass, localizePromise, [
      {
        id: "music",
        url_path: "music",
        require_admin: false,
        show_in_sidebar: true,
        icon: "mdi:music",
        title: "Music",
        mode: "storage",
      },
      {
        id: "cameras",
        url_path: "cameras",
        require_admin: false,
        show_in_sidebar: true,
        icon: "mdi:cctv",
        title: "Cameras",
        mode: "storage",
      },
      {
        id: "garden",
        url_path: "garden",
        require_admin: false,
        show_in_sidebar: true,
        icon: "mdi:flower",
        title: "Garden",
        mode: "storage",
      },
      {
        id: "lovelace-advanced",
        url_path: "lovelace-advanced",
        require_admin: false,
        show_in_sidebar: false,
        icon: "mdi:cog",
        title: "Advanced",
        mode: "yaml",
        filename: "lovelace-advanced.yaml",
      },
    ]);
    mockAuth(hass);
    mockTranslations(hass, localizePromise);
    mockHistory(hass);
    mockRecorder(hass);
    mockTodo(hass);
    mockSensor(hass);
    mockSystemLog(hass);
    mockTemplate(hass);
    mockEvents(hass);
    mockMediaPlayer(hass);
    mockFrontend(hass);
    mockHardware(hass);
    mockHassioSupervisor(hass);
    mockIcons(hass);
    mockEnergy(hass);
    mockPersistentNotification(hass);
    // Consumed app-wide via the lazy manifests context, so register eagerly.
    mockIntegration(hass);
    // Config panel mocks are code-split: the loader runs (and the chunk is
    // dynamically imported) the first time one of these config-only WS/REST
    // commands is requested, i.e. when the config panel is opened.
    hass.mockLazyLoad(
      (command) => CONFIG_PANEL_COMMANDS.some((p) => command.startsWith(p)),
      () =>
        import("./stubs/config-panel").then((mod) => mod.mockConfigPanel(hass))
    );
    mockAreaRegistry(hass);
    mockDeviceRegistry(hass, demoDevices);
    mockFloorRegistry(hass);
    mockLabelRegistry(hass, [
      {
        label_id: "kitchen",
        name: "Kitchen",
        icon: null,
        color: "red",
        description: "Everything in the kitchen",
        created_at: 1735689600,
        modified_at: 1735689600,
      },
      {
        label_id: "garden",
        name: "Garden",
        icon: "mdi:flower",
        color: "green",
        description: null,
        created_at: 1735689600,
        modified_at: 1735689600,
      },
      {
        label_id: "battery-powered",
        name: "Battery powered",
        icon: null,
        color: "orange",
        description: "Runs on batteries",
        created_at: 1735689600,
        modified_at: 1735689600,
      },
    ]);
    mockUsagePrediction(hass);
    mockEntityRegistryDisplay(hass);
    mockEntityRegistry(hass, [
      {
        config_entry_id: "co2signal",
        config_subentry_id: null,
        device_id: "co2signal",
        area_id: null,
        disabled_by: null,
        entity_id: "sensor.co2_intensity",
        id: "sensor.co2_intensity",
        name: null,
        icon: null,
        labels: [],
        categories: {},
        platform: "co2signal",
        hidden_by: null,
        entity_category: null,
        has_entity_name: false,
        unique_id: "co2_intensity",
        options: null,
        created_at: 0,
        modified_at: 0,
      },
      {
        config_entry_id: "co2signal",
        config_subentry_id: null,
        device_id: "co2signal",
        area_id: null,
        disabled_by: null,
        entity_id: "sensor.grid_fossil_fuel_percentage",
        id: "sensor.co2_intensity",
        name: null,
        icon: null,
        labels: [],
        categories: {},
        platform: "co2signal",
        hidden_by: null,
        entity_category: null,
        has_entity_name: false,
        unique_id: "grid_fossil_fuel_percentage",
        options: null,
        created_at: 0,
        modified_at: 0,
      },
      ...connectivityEntityRegistryEntries,
      ...zoneRegistryEntries,
    ]);

    hass.addEntities(energyEntities());
    hass.addEntities(connectivityEntities());

    // Once config is loaded AND localize, set registries, entities and theme.
    Promise.all([selectedDemoConfig, localizePromise]).then(
      async ([conf, localize]) => {
        setDemoFloors(hass, conf.floors);
        setDemoAreas(hass, conf.areas);
        hass.addEntities(demoConfigEntities(selectedDemo, conf, localize));
        installDemoStateHooks();
        registerDemoAutomations(hass, selectedDemo, conf);
        applyDemoTheme(hass, conf.theme);
        try {
          const response = await fetch("/api/ha-demo/state", {
            credentials: "same-origin",
          });
          if (!response.ok) throw new Error("Demo state is unavailable");
          const payload = await response.json();
          const restored = {};
          for (const [entityId, saved] of Object.entries(payload.states ?? {})) {
            // provideHass() returns its initial object; updateHass replaces it
            // when the live host state changes. Read the current host snapshot.
            const current = this.hass!.states[entityId];
            if (!current || !isDemoPersistedEntity(entityId)) continue;
            const mockEntity = hass.mockEntities[entityId];
            if (mockEntity && typeof mockEntity.update === "function") {
              // Keep the mock entity's own model in sync with the state map.
              // Its later update() calls republish to hass.states from toState().
              mockEntity.update({
                state: saved.state,
                attributes: saved.attributes ?? {},
              });
              restored[entityId] = mockEntity.toState();
            } else {
              restored[entityId] = {
                ...current,
                state: saved.state,
                attributes: { ...current.attributes, ...saved.attributes },
              };
            }
          }
          // Publish through the HA element so both the root property and its
          // grouped Lit context providers see the restored map.
          this._updateHass({
            states: { ...this.hass!.states, ...restored },
          });
          for (const entityId of DEMO_PERSISTED_ENTITY_IDS) {
            const state = this.hass!.states[entityId];
            if (state) {
              lastPersistedState[entityId] = JSON.stringify({
                state: state.state,
                brightness: state.attributes.brightness,
                temperature: state.attributes.temperature,
              });
            }
          }
          demoStateReady = true;
          hass.mockService("scene", "turn_on", async (data, target, demoHass) => {
            const ids = data?.entity_id ?? target?.entity_id;
            const sceneId = Array.isArray(ids) ? ids[0] : ids;
            const affected = sceneId === "scene.evening"
              ? [["light.living_room_floor_lamp", "on", 180], ["light.living_room_spotlights", "off"]]
              : sceneId === "scene.focus"
                ? [["light.office_desk_lamp", "on", 255], ["light.living_room_floor_lamp", "off"]]
                : [];
            if (!affected.length) return;
            const changes = {};
            for (const [entityId, state, brightness] of affected) {
              const old = demoHass.states[entityId];
              if (!old) continue;
              changes[entityId] = {
                ...old,
                state,
                attributes: {
                  ...old.attributes,
                  ...(brightness === undefined ? {} : { brightness }),
                },
              };
            }
            demoHass.updateStates(changes);
            for (const [entityId, state] of Object.entries(changes)) {
              lastPersistedState[entityId] = JSON.stringify({
                state: state.state,
                brightness: state.attributes.brightness,
                temperature: state.attributes.temperature,
              });
            }
            await persistDemoEntities(demoHass, Object.keys(changes));
          });
        } catch (error) {
          showDemoStateFailure(
            "Не удалось загрузить состояние демо. Проверьте соединение и попробуйте снова."
          );
          throw error;
        }
      }
    );

    // Taken from polymer/pwa-helpers. BSD-3 licensed
    document.body.addEventListener(
      "click",
      (e) => {
        const href = isNavigationClick(e);

        if (!href) {
          return;
        }

        e.preventDefault();
        navigate(href);
      },
      { capture: true }
    );

    (this as any).hassConnected();
  }
}

const DEMO_PERSISTED_ENTITY_IDS = [
  "climate.living_room",
  "light.living_room_floor_lamp",
  "light.living_room_spotlights",
  "light.kitchen_spotlights",
  "light.kitchen_worktop",
  "light.entrance_ceiling",
  "light.bedroom_ceiling",
  "light.bedside_lamp",
  "light.office_desk_lamp",
  "light.garden_path",
];

const isDemoPersistedEntity = (entityId: string) =>
  DEMO_PERSISTED_ENTITY_IDS.includes(entityId);

const showDemoStateFailure = (message: string) => {
  if (document.querySelector("[data-ha-demo-state-failure]")) return;
  const panel = document.createElement("main");
  panel.dataset.haDemoStateFailure = "true";
  panel.setAttribute("role", "alert");
  panel.setAttribute("aria-live", "assertive");
  panel.style.cssText =
    "position:fixed;inset:0;z-index:2147483647;display:grid;place-items:center;padding:24px;box-sizing:border-box;background:var(--primary-background-color,#f5f5f5);color:var(--primary-text-color,#212121);font:16px Roboto,Arial,sans-serif";
  const content = document.createElement("section");
  content.style.cssText =
    "max-width:440px;padding:28px;border-radius:12px;background:var(--card-background-color,#fff);box-shadow:0 4px 24px #0002;text-align:center";
  const heading = document.createElement("h1");
  heading.textContent = "Демо временно недоступно";
  heading.style.cssText = "font-size:22px;margin:0 0 12px";
  const detail = document.createElement("p");
  detail.textContent = message;
  detail.style.cssText = "line-height:1.5;margin:0 0 20px";
  const retry = document.createElement("button");
  retry.type = "button";
  retry.textContent = "Вернуться в демо";
  retry.style.cssText =
    "min-height:44px;padding:0 20px;border:0;border-radius:8px;background:#03a9f4;color:#fff;font:inherit;font-weight:600;cursor:pointer";
  retry.addEventListener("click", () => {
    retry.disabled = true;
    retry.textContent = "Загружаем демо…";
    // Clear only the demo app's transient route. The server-side saved state
    // remains untouched and is reloaded by the normal startup GET.
    window.location.replace(window.location.pathname);
  });
  content.append(heading, detail, retry);
  panel.append(content);
  document.body.replaceChildren(panel);
};

let demoPersistenceQueue: Promise<void> = Promise.resolve();

const persistDemoEntities = (hass: MockHomeAssistant, entityIds: string[]) => {
  const entities = entityIds.map((entity_id) => {
    const entity = hass.states[entity_id];
    return {
      entity_id,
      state: entity.state,
      ...(entity.attributes.brightness == null ? {} : { brightness: entity.attributes.brightness }),
      ...(entity.attributes.temperature == null ? {} : { temperature: entity.attributes.temperature }),
    };
  });
  const save = demoPersistenceQueue.then(async () => {
    let response: Response;
    try {
      response = await fetch("/api/ha-demo/state", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entities }),
      });
    } catch {
      showDemoStateFailure(
        "Изменение не сохранено. Проверьте соединение и попробуйте снова."
      );
      throw new Error("Home Assistant demo state write failed");
    }
    if (!response.ok) {
      showDemoStateFailure(
        "Изменение не сохранено. Проверьте соединение и попробуйте снова."
      );
      throw new Error("Home Assistant demo state write failed");
    }
    if (window.parent !== window) {
      window.parent.postMessage({ type: "habab-ha-demo-interaction" }, location.origin);
    }
  });
  demoPersistenceQueue = save.catch(() => undefined);
  return save;
};

declare global {
  interface HTMLElementTagNameMap {
    "ha-demo": HaDemo;
  }
}
