import assert from "node:assert/strict";
import test from "node:test";
import { ensureDeviceDashboard, vacuumFavorites } from "../src/habab_home/frontend/device-dashboard.js";

const states = {
  "vacuum.robot": { state: "docked", attributes: {} },
  "sensor.robot_battery": { state: "73", attributes: { device_class: "battery" } },
  "sensor.other_battery": { state: "99", attributes: { device_class: "battery" } },
};
const entries = [
  { entity_id: "vacuum.robot", device_id: "robot" },
  { entity_id: "sensor.robot_battery", device_id: "robot" },
  { entity_id: "sensor.other_battery", device_id: "other" },
];
const makeHass = (config = {}) => {
  const calls = [];
  let home = structuredClone(config);
  const hass = {
    user: { is_admin: true }, panels: { home: { component_name: "home", url_path: "home" } },
    states, async callWS(message) {
      calls.push(message);
      if (message.type === "config/entity_registry/list") return entries;
      if (message.type === "frontend/get_system_data") return { value: structuredClone(home) };
      if (message.type === "frontend/set_system_data") { home = structuredClone(message.value); return {}; }
      throw new Error(`Unexpected command: ${message.type}`);
    },
  };
  return { hass, calls, home: () => home };
};

test("pins the verified vacuum and its own battery without creating another dashboard", async () => {
  const original = { favorite_entities: ["light.kitchen"], hide_suggested_entities: false,
    shortcuts: [{ path: "/energy" }], welcome_banner_dismissed: true };
  const fixture = makeHass(original);
  assert.deepEqual(await ensureDeviceDashboard(fixture.hass), { url: "/home/overview", count: 1 });
  await ensureDeviceDashboard(fixture.hass);
  assert.deepEqual(fixture.home(), { ...original,
    favorite_entities: ["light.kitchen", "vacuum.robot", "sensor.robot_battery"] });
  assert.equal(fixture.calls.filter((m) => m.type === "frontend/set_system_data").length, 1);
  assert.equal(fixture.calls.some((m) => m.type.startsWith("lovelace/")), false);
});

test("hidden, disabled, stale and unrelated entities never become device favorites", () => {
  const fixtureStates = { ...states, "vacuum.hidden": { state: "idle" },
    "vacuum.disabled": { state: "idle" }, "vacuum.orphan": { state: "idle" },
    "sensor.hidden_battery": { state: "50", attributes: { device_class: "battery" } } };
  const fixtureEntries = [
    { entity_id: "vacuum.hidden", hidden_by: "user" },
    { entity_id: "vacuum.disabled", disabled_by: "user" },
    { entity_id: "vacuum.missing" },
    { entity_id: "sensor.hidden_battery", device_id: "robot", hidden_by: "user" }, ...entries,
  ];
  assert.deepEqual(vacuumFavorites(fixtureStates, fixtureEntries), {
    ids: ["vacuum.robot", "sensor.robot_battery"], count: 1,
  });
});

test("unavailable battery does not become a false zero reading", () => {
  const fixtureStates = { ...states, "sensor.robot_battery": { state: "unavailable",
    attributes: { device_class: "battery" } } };
  assert.deepEqual(vacuumFavorites(fixtureStates, entries), { ids: ["vacuum.robot"], count: 1 });
});

test("no vacuum leaves all Home settings untouched", async () => {
  const fixture = makeHass({ favorite_entities: ["light.kitchen"] });
  fixture.hass.states = {};
  assert.deepEqual(await ensureDeviceDashboard(fixture.hass), { url: "/home/overview", count: 0 });
  assert.equal(fixture.calls.some((m) => m.type === "frontend/set_system_data"), false);
});

test("administrator and existing Home panel are required before any write", async () => {
  for (const patch of [{ user: { is_admin: false } }, { panels: {} }]) {
    const fixture = makeHass(); Object.assign(fixture.hass, patch);
    await assert.rejects(ensureDeviceDashboard(fixture.hass));
    assert.deepEqual(fixture.calls, []);
  }
});

test("invalid persisted favorite configuration is not overwritten", async () => {
  const fixture = makeHass({ favorite_entities: "invalid" });
  await assert.rejects(ensureDeviceDashboard(fixture.hass), /invalid_home_config/);
  assert.equal(fixture.calls.some((m) => m.type === "frontend/set_system_data"), false);
});

test("failed Home save remains a failure", async () => {
  const fixture = makeHass(); const call = fixture.hass.callWS;
  fixture.hass.callWS = (message) => message.type === "frontend/set_system_data"
    ? Promise.reject(new Error("save_failed")) : call(message);
  await assert.rejects(ensureDeviceDashboard(fixture.hass), /save_failed/);
  assert.deepEqual(fixture.home(), {});
});
