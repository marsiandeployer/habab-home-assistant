import assert from "node:assert/strict";
import test from "node:test";
import { buildVacuumCards, ensureDeviceDashboard } from "../src/habab_home/frontend/device-dashboard.js";

const state = (entity_id, attributes = {}, stateValue = "docked") => ({
  entity_id,
  state: stateValue,
  attributes,
});

test("dashboard update is idempotent and preserves user views and cards", async () => {
  const userCard = { type: "markdown", content: "Keep this card" };
  const userView = { path: "overview", title: "Overview", cards: [userCard] };
  const ownedView = { path: "vacuum", title: "My vacuum", cards: [{ id: "habab-vacuum-picture-vacuum.old", type: "picture-entity" }, { type: "entities", entities: [] }] };
  let savedConfig = { views: [userView, ownedView], background: "keep" };
  const calls = [];
  const hass = {
    user: { is_admin: true },
    locale: { language: "ru" },
    states: { "vacuum.living_room": state("vacuum.living_room", { supported_features: 4 | 8 | 16 | 1024, battery_level: 73 }) },
    async callWS(message) {
      calls.push(message);
      switch (message.type) {
        case "lovelace/dashboards/list": return [{ url_path: "habab-devices", mode: "storage", title: "Мои устройства" }];
        case "lovelace/config": return structuredClone(savedConfig);
        case "config/entity_registry/list": return [{ entity_id: "vacuum.living_room", device_id: "vacuum-device", name: "Living room" }];
        case "config/device_registry/list": return [{ id: "vacuum-device", manufacturer: "Example", model: "ijai.vacuum.v17", name: "Vacuum", name_by_user: "S10" }];
        case "lovelace/config/save": savedConfig = structuredClone(message.config); return {};
        default: throw new Error(`Unexpected WebSocket command: ${message.type}`);
      }
    },
  };

  await ensureDeviceDashboard(hass);
  await ensureDeviceDashboard(hass);
  assert.equal(calls.filter(({ type }) => type === "lovelace/config/save").length, 2);
  assert.deepEqual(savedConfig.views[0], userView);
  assert.equal(savedConfig.background, "keep");
  const view = savedConfig.views[1];
  assert.equal(view.title, "My vacuum");
  assert.deepEqual(view.cards.slice(0, 1), [{ type: "entities", entities: [] }]);
  assert.equal(view.cards.filter((card) => card.id?.startsWith("habab-vacuum-picture-")).length, 1);
  assert.equal(view.cards.filter((card) => card.id?.startsWith("habab-vacuum-controls-")).length, 1);
  const picture = view.cards.find((card) => card.type === "picture-entity");
  const tile = view.cards.find((card) => card.type === "tile");
  assert.equal(picture.entity, "vacuum.living_room");
  assert.equal(picture.name, "S10");
  assert.equal(picture.image, "/habab-home/assets/robot-vacuum.svg");
  assert.deepEqual(tile.features, [{ type: "vacuum-commands", commands: ["start_pause", "stop", "clean_spot", "return_home"] }]);
  assert.deepEqual(tile.state_content, ["state", "battery_level"]);
});

test("unknown battery values are omitted instead of rendered as a zero reading", () => {
  const cards = buildVacuumCards(
    { "vacuum.hall": state("vacuum.hall", { battery_level: "unknown", supported_features: 0 }) },
    [{ entity_id: "vacuum.hall", device_id: "vacuum-device" }],
    [{ id: "vacuum-device", model: "S10" }],
  );
  const tile = cards.find((card) => card.type === "tile");
  assert.equal(Object.hasOwn(tile, "state_content"), false);
});

test("first setup creates the managed storage dashboard and does not invent vacuum cards", async () => {
  const calls = [];
  const hass = {
    user: { is_admin: true }, locale: { language: "ru" }, states: {},
    async callWS(message) {
      calls.push(message);
      switch (message.type) {
        case "lovelace/dashboards/list": return [];
        case "lovelace/dashboards/create": return { url_path: message.url_path, mode: message.mode };
        case "lovelace/config": return { views: [] };
        case "config/entity_registry/list": return [];
        case "config/device_registry/list": return [];
        case "lovelace/config/save": return {};
        default: throw new Error(`Unexpected WebSocket command: ${message.type}`);
      }
    },
  };
  const result = await ensureDeviceDashboard(hass);
  const create = calls.find(({ type }) => type === "lovelace/dashboards/create");
  assert.equal(create.url_path, "habab-devices");
  assert.equal(create.title, "Мои устройства");
  assert.equal(create.mode, "storage");
  assert.equal(result.count, 0);
  const emptyCards = calls.find(({ type }) => type === "lovelace/config/save").config.views[0].cards;
  assert.equal(emptyCards.length, 1);
  assert.equal(emptyCards[0].type, "markdown");
  assert.match(emptyCards[0].content, /не найдены/);
});
