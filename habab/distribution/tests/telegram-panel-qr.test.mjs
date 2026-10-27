import assert from "node:assert/strict";
import test from "node:test";

const registry = new Map();
globalThis.HTMLElement = class {
  attachShadow() { this.shadowRoot = { firstChild: null, querySelector: () => null }; }
};
globalThis.customElements = {
  get: (name) => registry.get(name),
  define: (name, value) => registry.set(name, value),
};
await import("../src/habab_home/frontend/telegram-panel.js");
const Panel = registry.get("habab-telegram-panel");

function statePanel(state, group = { phase: "not_configured" }) {
  const panel = Object.create(Panel.prototype);
  Object.assign(panel, {
    state,
    group,
    groupPairingUrl: "",
    ownerGeneration: 0,
    busy: false,
    language: "en",
    _hass: { user: { id: "owner-a" }, connection: {} },
  });
  return panel;
}

test("only current owner pairing state can expose its QR payload", () => {
  const url = "https://t.me/habab_home_bot?start=owner_nonce-123";
  const panel = statePanel({ phase: "pairing", pairing_url: url });
  assert.equal(panel.currentOwnerPairingUrl(), url);
  panel.state = { phase: "connected" };
  assert.equal(panel.currentOwnerPairingUrl(), "");
});

test("group QR disappears as soon as server state expires or consumes the pairing link", async () => {
  const url = "https://t.me/habab_home_bot?startgroup=group_nonce-456";
  const panel = statePanel({ phase: "connected" }, { phase: "pairing" });
  panel.groupPairingUrl = url;
  assert.equal(panel.currentGroupPairingUrl(), url);
  panel.group = { phase: "awaiting_admin" };
  assert.equal(panel.currentGroupPairingUrl(), "");
  panel.group = { phase: "pairing" };
  panel.request = async () => ({ phase: "connected", group: { phase: "not_configured" } });
  await panel.loadStatus();
  assert.equal(panel.groupPairingUrl, "");
  assert.equal(panel.currentGroupPairingUrl(), "");
});

test("switching Home Assistant owners clears both pairing URLs and QR generations", () => {
  const panel = new Panel();
  panel.language = "en";
  panel._hass = { user: { id: "owner-a" }, connection: {} };
  panel.state = { phase: "pairing", pairing_url: "https://t.me/habab_home_bot?start=old-owner" };
  panel.group = { phase: "pairing" };
  panel.groupPairingUrl = "https://t.me/habab_home_bot?startgroup=old-owner";
  panel.qrUrls = { owner: panel.state.pairing_url, group: panel.groupPairingUrl };

  panel.hass = { user: { id: "owner-b" }, connection: {}, locale: { language: "en" } };

  assert.deepEqual(panel.state, { phase: "not_configured" });
  assert.deepEqual(panel.group, { phase: "unsupported" });
  assert.equal(panel.groupPairingUrl, "");
  assert.deepEqual(panel.qrUrls, { owner: "", group: "" });
  assert.equal(panel.currentOwnerPairingUrl(), "");
  assert.equal(panel.currentGroupPairingUrl(), "");
  assert.equal(panel.ownerGeneration, 1);
});

test("copy failure is shown on the pairing panel", async () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { clipboard: { writeText: async () => { throw new Error("permission_denied"); } } },
  });
  try {
    const panel = statePanel({ phase: "pairing", pairing_url: "https://t.me/habab_home_bot?start=owner_nonce-123" });
    const status = { textContent: "", dataset: {} };
    panel.q = (selector) => selector === "#owner-pairing-copy-status" ? status : null;
    await panel.copyPairingUrl("owner");
    assert.equal(status.textContent, "Could not copy the link. Open it manually.");
    assert.equal(status.dataset.error, "true");
  } finally {
    if (original) Object.defineProperty(globalThis, "navigator", original);
    else delete globalThis.navigator;
  }
});
