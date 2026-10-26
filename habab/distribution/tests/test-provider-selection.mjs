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
const { assistantProviderPresentation, assistantRoutingSummary, providerModelPresentation, strings } = await Promise.all([
  import("../src/habab_home/frontend/ai-panel.js?provider-selection-test"),
  import("../src/habab_home/frontend/strings.js?provider-selection-test"),
]).then(([panelModule, stringModule]) => ({
  assistantProviderPresentation: panelModule.assistantProviderPresentation,
  assistantRoutingSummary: panelModule.assistantRoutingSummary,
  providerModelPresentation: panelModule.providerModelPresentation,
  strings: stringModule.strings,
}));
const HababAIPanel = definitions.get("habab-ai-panel");
if (originalHTMLElement) Object.defineProperty(globalThis, "HTMLElement", originalHTMLElement);
else delete globalThis.HTMLElement;
if (originalCustomElements) Object.defineProperty(globalThis, "customElements", originalCustomElements);
else delete globalThis.customElements;

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function panelForKeyFetch(provider, entry, keyRequests) {
  const nodes = new Map();
  const makeNode = () => ({
    value: "", type: "password", textContent: "", href: "", hidden: false,
    attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    replaceChildren() {},
    querySelector() { return makeNode(); },
  });
  const panel = Object.create(HababAIPanel.prototype);
  Object.assign(panel, {
    isConnected: true,
    provider,
    connections: [entry],
    keyVisibilityPreference: null,
    keyVisibilityProvider: null,
    savedKeyLoaded: false,
    keyEdited: false,
    modelEdited: false,
    keyFetchGeneration: 0,
    _hass: { callWS: () => keyRequests.shift().promise },
    shadowRoot: { querySelectorAll: () => [] },
    q(selector) {
      if (!nodes.has(selector)) nodes.set(selector, makeNode());
      return nodes.get(selector);
    },
    entry() { return this.connections.find((item) => item.provider === this.provider); },
    updateControls() {},
    t: (key) => key.endsWith("Guide") ? [] : key,
    feedback: (message) => { panel.lastFeedback = message; },
  });
  return { panel, nodes };
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

test("managed Habab presentation keeps the saved compatibility value and gateway alias distinct", () => {
  const managed = providerModelPresentation("habab", {
    model: "gemini-3.1-flash-lite",
    managed_model: "habab-chat",
    effective_model: "habab-chat",
    protocol: "openai_compatible",
    text_ready: true,
  }, "en");
  assert.equal(managed.readOnly, true);
  assert.equal(managed.label, "Saved compatibility value");
  assert.match(managed.help, /Gateway alias: habab-chat/);
  assert.match(managed.help, /OpenAI-compatible API/);
  assert.match(managed.help, /upstream model is not reported/i);
  assert.doesNotMatch(managed.help, /gemini-3\.1-flash-lite/i);
  assert.doesNotMatch(managed.help, /current model is habab-chat/i);

  const managedSelected = assistantProviderPresentation({
    provider: "habab",
    model: "gemini-3.1-flash-lite",
    managed_model: "habab-chat",
    effective_model: null,
  });
  assert.deepEqual(managedSelected, { key: "currentAssistantManaged" });
  assert.match(strings.en[managedSelected.key], /Habab selects the model/i);
  assert.doesNotMatch(strings.en[managedSelected.key], /habab-chat|gemini-3\.1-flash-lite/i);

  assert.deepEqual(assistantProviderPresentation({
    provider: "habab", managed_model: "habab-chat", text_ready: true,
    configured_upstream_provider: "deepseek", configured_upstream_model: "deepseek-flash",
  }), { key: "currentAssistantManagedRoute", provider: "DeepSeek", model: "deepseek-flash" });

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

  assert.deepEqual(assistantProviderPresentation({
    provider: "openai", managed_model: null, effective_model: "gpt-4.1-mini",
  }), { key: "currentAssistant", model: "gpt-4.1-mini" });
  assert.deepEqual(providerModelPresentation("deepseek", { model: "deepseek-flash" }, "en"), {
    readOnly: false,
    label: null,
    help: null,
  });
});

test("routing summary separates managed text, default voice, Android wake detection, and configured TTS", () => {
  const entry = {
    provider: "habab",
    model: "gemini-3.1-flash-lite",
    managed_model: "habab-chat",
    effective_model: null,
    text_ready: true,
    configured_upstream_provider: "deepseek",
    configured_upstream_model: "deepseek-flash",
    voice_provider: "habab",
    voice_protocol: "gemini_live",
    default_voice_model: "gemini-3.8-live",
    voice_ready: true,
  };
  const withoutTTS = assistantRoutingSummary(entry, [{ tts_engine: null }], "en");
  assert.equal(withoutTTS.length, 3);
  assert.match(withoutTTS[0], /Text chat: OpenCode → Habab → DeepSeek \(deepseek-flash\)/);
  assert.doesNotMatch(withoutTTS[0], /habab-chat|gemini-3\.1-flash-lite/);
  assert.match(withoutTTS[1], /Voice conversation: Habab → Gemini Live, default model gemini-3\.8-live/);
  assert.match(withoutTTS[2], /Android wake phrase: local, Vosk/);
  assert.doesNotMatch(withoutTTS.join(" "), /gemini-3\.1-flash-lite/i);

  const unreportedRoute = assistantRoutingSummary({ ...entry, configured_upstream_provider: null }, [], "en");
  assert.match(unreportedRoute[0], /Habab selects the model/);
  assert.doesNotMatch(unreportedRoute[0], /DeepSeek|habab-chat/);

  const withTTS = assistantRoutingSummary(entry, [{ tts_engine: "tts.google_translate_say" }], "en");
  assert.equal(withTTS.length, 4);
  assert.match(withTTS[3], /Separate TTS in Assist: tts\.google_translate_say/);
});

test("managed Habab API status describes the route, while direct provider status stays specific", () => {
  const nodes = new Map();
  const makeNode = () => ({ textContent: "", disabled: false, dataset: {}, value: "" });
  const run = (entry) => {
    const panel = Object.create(HababAIPanel.prototype);
    Object.assign(panel, {
      provider: entry.provider,
      connections: [entry],
      connectionsLoaded: true,
      connectionCheckStates: new Map(),
      busy: false,
      feedbackDeliveryState: { enabled: false, credential_configured: false, ready: false },
      q: (selector) => {
        if (!nodes.has(selector)) nodes.set(selector, makeNode());
        return nodes.get(selector);
      },
      entry: () => entry,
      checkStateKey: () => "synthetic-entry:model",
      checkTime: () => null,
      t: (key) => strings.en[key],
      applyFeedbackDeliveryControls: () => undefined,
      shadowRoot: { querySelectorAll: () => [] },
    });
    const previousWindow = globalThis.window;
    globalThis.window = { hababAssistant: { available: false } };
    try { panel.updateControls(); } finally {
      if (previousWindow === undefined) delete globalThis.window;
      else globalThis.window = previousWindow;
    }
    return {
      assistantStatus: nodes.get("#assistant-provider-status").textContent,
      apiStatus: nodes.get("#api-status").textContent,
    };
  };

  const managed = run({
    provider: "habab", assistant_selected: true, entry_id: "synthetic", model: "gemini-3.1-flash-lite",
    managed_model: "habab-chat", effective_model: null, protocol: "openai_compatible",
    text_ready: true, configured_upstream_provider: "deepseek",
    configured_upstream_model: "deepseek-flash", api_check_status: "verified",
  });
  assert.match(managed.assistantStatus, /Text chat: OpenCode → Habab → DeepSeek \(deepseek-flash\)/);
  assert.doesNotMatch(managed.assistantStatus, /habab-chat|gemini-3\.1-flash-lite/i);
  assert.doesNotMatch(managed.assistantStatus, /gemini-3\.1-flash-lite|model habab-chat/i);
  assert.equal(managed.apiStatus, strings.en.apiVerifiedManaged);
  assert.doesNotMatch(managed.apiStatus, /model/i);

  nodes.clear();
  const direct = run({
    provider: "openai", assistant_selected: true, entry_id: "synthetic-openai", model: "gpt-4.1-mini",
    managed_model: null, effective_model: "gpt-4.1-mini", protocol: "openai_compatible",
    api_check_status: "verified",
  });
  assert.match(direct.assistantStatus, /Text chat: OpenCode → OpenAI, model gpt-4\.1-mini/);
  assert.equal(direct.apiStatus, strings.en.apiVerified);
});

test("an explicit hide preference survives a same-provider saved-key refresh", async () => {
  const initialRequest = deferred();
  const refreshRequest = deferred();
  const entry = { provider: "habab", entry_id: "synthetic-habab" };
  const { panel } = panelForKeyFetch("habab", entry, [initialRequest, refreshRequest]);

  panel.selectProvider();
  initialRequest.resolve({ api_key: "synthetic-saved-key-fixture" });
  await Promise.resolve();
  assert.equal(panel.q("#key").type, "text");

  panel.keyVisibilityPreference = "hidden";
  panel.applyKeyVisibility();
  panel.selectProvider({ preserveDraft: true });
  refreshRequest.resolve({ api_key: "synthetic-saved-key-fixture" });
  await Promise.resolve();

  assert.equal(panel.q("#key").value, "synthetic-saved-key-fixture");
  assert.equal(panel.savedKeyLoaded, true);
  assert.equal(panel.q("#key").type, "password");
  assert.equal(panel.q("#reveal").attributes["aria-pressed"], "false");
});

test("switching providers resets the visibility choice and preserves the saved-key default", async () => {
  const request = deferred();
  const entry = { provider: "deepseek", entry_id: "synthetic-deepseek" };
  const { panel } = panelForKeyFetch("deepseek", entry, [request]);
  panel.keyVisibilityProvider = "habab";
  panel.keyVisibilityPreference = "hidden";
  panel.savedKeyLoaded = true;

  panel.selectProvider();
  assert.equal(panel.keyVisibilityPreference, null);
  assert.equal(panel.savedKeyLoaded, false);
  request.resolve({ api_key: "synthetic-switched-provider-key" });
  await Promise.resolve();

  assert.equal(panel.q("#key").value, "synthetic-switched-provider-key");
  assert.equal(panel.q("#key").type, "text");
  assert.equal(panel.q("#reveal").attributes["aria-pressed"], "true");
});

test("an unsaved key draft remains masked during same-provider refresh", () => {
  const entry = { provider: "habab", entry_id: "synthetic-habab" };
  const { panel } = panelForKeyFetch("habab", entry, []);
  panel.keyVisibilityProvider = "habab";
  panel.keyEdited = true;
  panel.q("#key").value = "synthetic-unsaved-key-fixture";
  panel.selectProvider({ preserveDraft: true });

  assert.equal(panel.q("#key").value, "synthetic-unsaved-key-fixture");
  assert.equal(panel.q("#key").type, "password");
  assert.equal(panel.savedKeyLoaded, false);
});

test("a stale saved-key rejection after switching providers is ignored", async () => {
  const oldRequest = deferred();
  const newRequest = deferred();
  const oldEntry = { provider: "habab", entry_id: "synthetic-old" };
  const newEntry = { provider: "openai", entry_id: "synthetic-new" };
  const { panel } = panelForKeyFetch("habab", oldEntry, [oldRequest, newRequest]);
  const oldFetch = panel.selectProvider();

  panel.provider = "openai";
  panel.connections = [oldEntry, newEntry];
  const newFetch = panel.selectProvider();
  oldRequest.reject({ code: "synthetic_stale_failure" });
  await oldFetch;
  assert.equal(panel.lastFeedback, undefined);
  assert.equal(panel.q("#key").value, "");

  newRequest.resolve({ api_key: "synthetic-current-key" });
  await newFetch;
  assert.equal(panel.q("#key").value, "synthetic-current-key");
});
