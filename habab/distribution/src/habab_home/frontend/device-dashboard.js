// Device setup uses the existing Home dashboard and its native favorite cards.
export function vacuumFavorites(states, entityEntries) {
  const visible = (entry) => !entry.disabled_by && !entry.hidden_by && states[entry.entity_id];
  const vacuums = entityEntries.filter((entry) =>
    entry.entity_id.startsWith("vacuum.") && visible(entry)
  );
  const ids = [];
  for (const vacuum of vacuums) {
    ids.push(vacuum.entity_id);
    if (!vacuum.device_id) continue;
    const battery = entityEntries.find((entry) =>
      entry.device_id === vacuum.device_id && entry.entity_id.startsWith("sensor.") &&
      visible(entry) && states[entry.entity_id].state !== "unavailable" &&
      (entry.device_class === "battery" || entry.original_device_class === "battery" ||
        states[entry.entity_id].attributes?.device_class === "battery")
    );
    if (battery) ids.push(battery.entity_id);
  }
  return { ids: [...new Set(ids)], count: vacuums.length };
}

export async function ensureDeviceDashboard(hass) {
  if (!hass?.user?.is_admin) throw new Error("admin_required");
  const panel = Object.values(hass.panels || {}).find((item) => item.component_name === "home");
  if (!panel?.url_path) throw new Error("home_panel_unavailable");
  const url = `/${panel.url_path}/overview`;
  const entities = await hass.callWS({ type: "config/entity_registry/list" });
  const selection = vacuumFavorites(hass.states || {}, entities);
  if (!selection.count) return { url, count: 0 };

  const response = await hass.callWS({ type: "frontend/get_system_data", key: "home" });
  const config = response.value ?? {};
  if (typeof config !== "object" || Array.isArray(config) ||
      (config.favorite_entities !== undefined && !Array.isArray(config.favorite_entities))) {
    throw new Error("invalid_home_config");
  }
  const favorites = config.favorite_entities || [];
  const missing = selection.ids.filter((id) => !favorites.includes(id));
  if (missing.length) {
    await hass.callWS({ type: "frontend/set_system_data", key: "home",
      value: { ...config, favorite_entities: [...favorites, ...missing] } });
  }
  return { url, count: selection.count };
}

export async function openDevices(hass) {
  const result = await ensureDeviceDashboard(hass);
  history.pushState(history.state, "", result.url);
  window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  return result;
}
