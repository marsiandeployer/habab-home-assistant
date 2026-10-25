import assert from "node:assert/strict";
import test from "node:test";

const originalHTMLElement = Object.getOwnPropertyDescriptor(globalThis, "HTMLElement");
const originalCustomElements = Object.getOwnPropertyDescriptor(globalThis, "customElements");
const definitions = new Map();
Object.defineProperty(globalThis, "HTMLElement", { configurable: true, value: class HTMLElementStub {} });
Object.defineProperty(globalThis, "customElements", {
  configurable: true,
  value: { get: (name) => definitions.get(name), define: (name, value) => definitions.set(name, value) },
});
const { providerModelPresentation } = await import("../src/habab_home/frontend/ai-panel.js?provider-selection-test");
const HababAIPanel = definitions.get("habab-ai-panel");
if (originalHTMLElement) Object.defineProperty(globalThis, "HTMLElement", originalHTMLElement);
else delete globalThis.HTMLElement;
if (originalCustomElements) Object.defineProperty(globalThis, "customElements", originalCustomElements);
else delete globalThis.customElements;

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function panelWithPendingProviderList(providerList) {
  const panel = Object.create(HababAIPanel.prototype);
  const pipelineStatus = { textContent: "" };
  Object.assign(panel, {
    provider: "openai",
    providerSelectionExplicit: false,
    initialProviderSelectionApplied: false,
    connections: [],
    connectionsLoaded: false,
    keyEdited: false,
    modelEdited: false,
    _hass: {
      callWS: ({ type }) => type === "habab_home/providers/list"
        ? providerList.promise
        : Promise.resolve({ pipelines: [] }),
    },
    q: (selector) => selector === "#pipeline-status" ? pipelineStatus : { hidden: true },
    selectProvider: () => undefined,
    updateControls: () => undefined,
    feedback: () => undefined,
    t: (key) => key,
  });
  return panel;
}

test("initial selected provider does not capture a key or model draft typed during load", async () => {
  const providerList = deferred();
  const panel = panelWithPendingProviderList(providerList);
  const refresh = panel.refresh();
  panel.keyEdited = true;
  panel.modelEdited = true;
  providerList.resolve({ connections: [{ provider: "habab", assistant_selected: true } ] });
  await refresh;
  assert.equal(panel.provider, "openai");
  assert.equal(panel.initialProviderSelectionApplied, true);
});

test("feedback receiver password draft survives a panel rerender", () => {
  const panel = Object.create(HababAIPanel.prototype);
  const nodes = new Map();
  const node = () => {
    const item = {
      listeners: new Map(), value: "", dataset: {}, selectionStart: 0,
      selectionEnd: 0, selectionDirection: "none",
      addEventListener(name, listener) { this.listeners.set(name, listener); },
      focus() { panel.shadowRoot.activeElement = this; },
      setSelectionRange(start, end, direction) {
        this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction;
      },
      setAttribute() {}, querySelector() { return node(); },
    };
    return item;
  };
  panel.shadowRoot = {
    activeElement: null,
    set innerHTML(_value) { nodes.clear(); this.activeElement = null; },
    querySelector(selector) {
      if (!nodes.has(selector)) {
        const item = node();
        item.id = selector.startsWith("#") ? selector.slice(1) : "";
        nodes.set(selector, item);
      }
      return nodes.get(selector);
    },
  };
  Object.assign(panel, {
    _hass: {}, language: "en", provider: "openai", busy: false,
    feedbackCredentialDraft: "", feedbackDeliveryState: {
      enabled: false, credential_configured: false, ready: false,
    },
    selectProvider: () => undefined,
    t: (key) => key,
    q(selector) { return this.shadowRoot.querySelector(selector); },
  });

  panel.render();
  const firstInput = panel.q("#feedback-delivery-token");
  firstInput.listeners.get("input")({ target: { value: "unsaved-fixture-token" } });
  firstInput.selectionStart = 8;
  firstInput.selectionEnd = 14;
  firstInput.selectionDirection = "backward";
  panel.shadowRoot.activeElement = firstInput;
  panel.render();

  const restoredInput = panel.q("#feedback-delivery-token");
  assert.equal(restoredInput.value, "unsaved-fixture-token");
  assert.equal(panel.shadowRoot.activeElement, restoredInput);
  assert.equal(restoredInput.selectionStart, 8);
  assert.equal(restoredInput.selectionEnd, 14);
  assert.equal(restoredInput.selectionDirection, "backward");
});

test("initial selected provider does not override a tab chosen during load", async () => {
  const providerList = deferred();
  const panel = panelWithPendingProviderList(providerList);
  const refresh = panel.refresh();
  panel.providerSelectionExplicit = true;
  panel.provider = "deepseek";
  providerList.resolve({ connections: [{ provider: "habab", assistant_selected: true } ] });
  await refresh;
  assert.equal(panel.provider, "deepseek");
});

test("a clean first load selects the saved provider once", async () => {
  const providerList = deferred();
  const panel = panelWithPendingProviderList(providerList);
  const initialRefresh = panel.refresh();
  providerList.resolve({ connections: [{ provider: "habab", assistant_selected: true } ] });
  await initialRefresh;
  assert.equal(panel.provider, "habab");

  panel._hass.callWS = ({ type }) => Promise.resolve(type === "habab_home/providers/list"
    ? { connections: [{ provider: "openai", assistant_selected: true } ] }
    : { pipelines: [] });
  await panel.refresh();
  assert.equal(panel.provider, "habab");
});

test("Habab model presentation follows effective metadata without changing stored legacy input", () => {
  const managed = providerModelPresentation("habab", {
    model: "gemini-3.1-flash-lite",
    managed_model: "habab-chat",
    effective_model: "habab-chat",
    protocol: "openai_compatible",
    text_ready: false,
  }, "ru");
  assert.equal(managed.readOnly, true);
  assert.equal(managed.label, "Сохранённое значение для совместимости");
  assert.match(managed.help, /habab-chat/);
  assert.match(managed.help, /OpenAI-совместимый API/);
  assert.match(managed.help, /не готово/);

  const legacy = providerModelPresentation("habab", {
    model: "gemini-3.1-flash-lite",
    managed_model: null,
    effective_model: "gemini-3.1-flash-lite",
    protocol: "google",
    text_ready: true,
  }, "en");
  assert.equal(legacy.readOnly, false);
  assert.match(legacy.help, /Google Gemini/);
  assert.match(legacy.help, /gemini-3.1-flash-lite/);

  assert.deepEqual(providerModelPresentation("deepseek", { model: "deepseek-flash" }, "en"), {
    readOnly: false,
    label: null,
    help: null,
  });
});
