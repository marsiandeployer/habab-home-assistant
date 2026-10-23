const DASHBOARD_PATH = "habab-devices";
const VIEW_PATH = "vacuum";
const CARD_ID_PREFIX = "habab-vacuum-";
const VACUUM_FEATURE = {
  pause: 4,
  stop: 8,
  return_home: 16,
  locate: 512,
  clean_spot: 1024,
  start: 8192,
};

const hasFeature = (state, flag) =>
  Number.isInteger(state.attributes?.supported_features) &&
  (state.attributes.supported_features & flag) === flag;

const vacuumCommands = (state) => {
  const features = state.attributes?.supported_features;
  if (!Number.isInteger(features)) return [];
  const commands = [];
  if (hasFeature(state, VACUUM_FEATURE.start) || hasFeature(state, VACUUM_FEATURE.pause)) {
    commands.push("start_pause");
  }
  if (hasFeature(state, VACUUM_FEATURE.stop)) commands.push("stop");
  if (hasFeature(state, VACUUM_FEATURE.clean_spot)) commands.push("clean_spot");
  if (hasFeature(state, VACUUM_FEATURE.locate)) commands.push("locate");
  if (hasFeature(state, VACUUM_FEATURE.return_home)) commands.push("return_home");
  return commands;
};

const cardId = (entityId, kind) =>
  `${CARD_ID_PREFIX}${kind}-${entityId.replace(/[^a-z0-9_-]/gi, "-")}`;

export function buildVacuumCards(states, entityEntries, deviceEntries, language = "ru") {
  const entitiesById = new Map(entityEntries.map((entry) => [entry.entity_id, entry]));
  const devicesById = new Map(deviceEntries.map((entry) => [entry.id, entry]));
  const genericName = language === "ru" ? "Робот-пылесос" : "Robot vacuum";
  const cards = [];

  for (const [entityId, state] of Object.entries(states)) {
    if (!entityId.startsWith("vacuum.")) continue;
    const registry = entitiesById.get(entityId);
    // An entity without a registry entry has no verified device identity.
    if (!registry || registry.disabled_by || registry.hidden_by) continue;
    const device = registry.device_id ? devicesById.get(registry.device_id) : undefined;
    const modelLabel = [device?.manufacturer, device?.model].filter(Boolean).join(" ");
    const label = device?.name_by_user || device?.name || registry.name || registry.original_name || modelLabel || genericName;
    const pictureId = cardId(entityId, "picture");
    const tileId = cardId(entityId, "controls");
    const image = "/habab-home/assets/robot-vacuum.svg";
    cards.push({
      id: pictureId,
      type: "picture-entity",
      entity: entityId,
      name: label,
      image,
      show_state: true,
      show_name: true,
      tap_action: { action: "more-info" },
    });

    const batteryLevel = state.attributes?.battery_level;
    const hasBattery = (typeof batteryLevel === "number" && Number.isFinite(batteryLevel)) ||
      (typeof batteryLevel === "string" && batteryLevel.trim() !== "" && Number.isFinite(Number(batteryLevel)));
    const controlCard = {
      id: tileId,
      type: "tile",
      entity: entityId,
      name: label,
      tap_action: { action: "more-info" },
      ...(hasBattery ? { state_content: ["state", "battery_level"] } : {}),
    };
    const commands = vacuumCommands(state);
    if (commands.length) {
      controlCard.features = [{ type: "vacuum-commands", commands }];
      controlCard.features_position = "bottom";
    }
    cards.push(controlCard);

    if (!hasBattery && device?.id) {
      const battery = entityEntries.find((entry) => entry.device_id === device.id &&
        entry.entity_id.startsWith("sensor.") &&
        (entry.device_class === "battery" || entry.original_device_class === "battery" ||
          states[entry.entity_id]?.attributes?.device_class === "battery") &&
        states[entry.entity_id] && states[entry.entity_id].state !== "unavailable");
      if (battery) {
        cards.push({
          id: cardId(entityId, `battery-${battery.entity_id}`),
          type: "tile",
          entity: battery.entity_id,
          name: battery.name || battery.original_name || (language === "ru" ? "Заряд батареи" : "Battery level"),
          tap_action: { action: "more-info" },
        });
      }
    }
  }
  return cards;
}

export function mergeVacuumView(config, cards, language = "ru") {
  if (!config || !Array.isArray(config.views)) throw new Error("invalid_lovelace_config");
  const views = [...config.views];
  const index = views.findIndex((view) => view.path === VIEW_PATH);
  const existing = index >= 0 ? views[index] : undefined;
  const retainedCards = (existing?.cards || []).filter(
    (card) => !String(card.id || "").startsWith(CARD_ID_PREFIX)
  );
  const view = {
    ...(existing || {}),
    title: existing?.title || (language === "ru" ? "Робот-пылесос" : "Robot vacuum"),
    path: VIEW_PATH,
    cards: [...retainedCards, ...cards],
  };
  if (index >= 0) views[index] = view;
  else views.push(view);
  return { ...config, views };
}

async function call(hass, type, fields = {}) {
  return hass.callWS({ type, ...fields });
}

export async function ensureDeviceDashboard(hass) {
  if (!hass?.user?.is_admin) throw new Error("admin_required");
  const dashboards = await call(hass, "lovelace/dashboards/list");
  let dashboard = dashboards.find((item) => item.url_path === DASHBOARD_PATH);
  let created = false;
  if (!dashboard) {
    dashboard = await call(hass, "lovelace/dashboards/create", {
      url_path: DASHBOARD_PATH,
      title: "Мои устройства",
      icon: "mdi:robot-vacuum",
      show_in_sidebar: true,
      require_admin: false,
      mode: "storage",
    });
    created = true;
  } else if (dashboard.mode !== "storage") {
    throw new Error("dashboard_not_storage");
  }

  let config;
  try {
    config = await call(hass, "lovelace/config", { url_path: DASHBOARD_PATH, force: true });
  } catch (error) {
    if (!created) throw error;
    config = { views: [] };
  }
  if (!Array.isArray(config?.views)) throw new Error("invalid_lovelace_config");

  const [entityEntries, deviceEntries] = await Promise.all([
    call(hass, "config/entity_registry/list"),
    call(hass, "config/device_registry/list"),
  ]);
  const cards = buildVacuumCards(hass.states || {}, entityEntries, deviceEntries, hass.locale?.language?.startsWith("ru") ? "ru" : "en");
  const language = hass.locale?.language?.startsWith("ru") ? "ru" : "en";
  const viewCards = cards.length ? cards : [{
    id: `${CARD_ID_PREFIX}empty`,
    type: "markdown",
    content: language === "ru"
      ? "### Роботы-пылесосы пока не найдены\n\nКогда Home Assistant добавит пылесос в реестр устройств, он появится здесь."
      : "### No robot vacuums found yet\n\nA vacuum will appear here after Home Assistant adds it to the device registry.",
  }];
  const merged = mergeVacuumView(config, viewCards, language);
  await call(hass, "lovelace/config/save", { url_path: DASHBOARD_PATH, config: merged });
  return { url: `/${DASHBOARD_PATH}/${VIEW_PATH}`, count: cards.filter((card) => card.type === "picture-entity").length };
}

export async function openDevices(hass) {
  const result = await ensureDeviceDashboard(hass);
  history.pushState(history.state, "", result.url);
  window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  return result;
}
