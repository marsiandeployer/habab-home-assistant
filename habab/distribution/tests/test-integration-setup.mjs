import assert from "node:assert/strict";
import test from "node:test";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

const globalNames = ["HTMLElement", "customElements", "window", "document", "CustomEvent"];
const saved = new Map(globalNames.map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
const definitions = new Map();

class ElementStub extends EventTarget {
  constructor(tagName = "div") {
    super();
    this.tagName = tagName;
    this.children = [];
    this.attributes = {};
    this.isConnected = true;
    this.hidden = false;
    this.disabled = false;
    this.textContent = "";
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = [...items]; }
  remove() { this.isConnected = false; }
  setAttribute(key, value) { this.attributes[key] = value; }
  querySelectorAll(selector) {
    return selector === "button" ? this.children.filter((child) => child.tagName === "button") : [];
  }
}

class HTMLElementStub extends ElementStub {
  constructor() {
    super("host");
    this.shadowRoot = {firstChild: {}, querySelector: () => undefined};
  }
  attachShadow() { return this.shadowRoot; }
}

class CustomEventStub extends Event {
  constructor(type, options = {}) { super(type, options); this.detail = options.detail; }
}

Object.defineProperty(globalThis, "HTMLElement", {configurable: true, value: HTMLElementStub});
Object.defineProperty(globalThis, "customElements", {configurable: true, value: {
  get: (name) => definitions.get(name), define: (name, value) => definitions.set(name, value),
}});
Object.defineProperty(globalThis, "window", {configurable: true, value: {}});
Object.defineProperty(globalThis, "document", {configurable: true, value: {
  createElement: (tag) => new ElementStub(tag),
}});
Object.defineProperty(globalThis, "CustomEvent", {configurable: true, value: CustomEventStub});
definitions.set("step-flow-form", class NativeFlowFormStub extends ElementStub {});
definitions.set("habab-native-form-context", class NativeFormContextStub extends ElementStub {});

const module = await import(`${pathToFileURL(resolve("habab/distribution/src/habab_home/frontend/integration-setup.js"))}?test`);

function setupShell(card) {
  card._ensureShell = () => {
    card._cardsRoot ||= new ElementStub("cards");
    card._statusError ||= new ElementStub("status-error");
  };
  card._ensureShell();
  return card;
}

function formRecord(overrides = {}) {
  return {
    id: "i1", flow_id: "native-flow", handler: "mqtt", translation_domain: "mqtt",
    domain: "mqtt", kind: "config", phase: "form", type: "form", step_id: "user",
    last_step: false, description_placeholders: {host: "broker.local"},
    data_schema: [{name: "password", type: "string"}], errors: {}, ...overrides,
  };
}

function installBridge(onLoad = () => ({
  tag: "step-flow-form", contextTag: "habab-native-form-context", flowConfig: {},
})) {
  const calls = [];
  globalThis.window.hababNativeUi = {
    version: 1,
    loadFlowForm: async (options) => { calls.push(options); return onLoad(options); },
  };
  return calls;
}

test("native StepFlowForm bridge is awaited and receives the protected native step", async () => {
  let resolveBridge;
  const bridgeCalls = installBridge(() => new Promise((resolve) => { resolveBridge = resolve; }));
  const card = setupShell(new module.HababIntegrationSetup());
  card._hass = {connected: true, language: "ru", async callWS() { return {interactions: []}; }};

  card._applyInteractions([formRecord()]);
  const record = card.cards.get("i1");
  assert.equal(record.form, undefined);
  assert.equal(bridgeCalls.length, 1);
  assert.equal(bridgeCalls[0].flowType, "config_flow");
  assert.equal(bridgeCalls[0].handler, "mqtt");
  assert.equal(bridgeCalls[0].domain, "mqtt");
  assert.deepEqual(bridgeCalls[0].step, {
    type: "form", flow_id: "i1", handler: "mqtt", step_id: "user",
    data_schema: [{name: "password", type: "string"}], errors: {},
    translation_domain: "mqtt", last_step: false,
    description_placeholders: {host: "broker.local"},
  });

  resolveBridge({tag: "step-flow-form", contextTag: "habab-native-form-context", flowConfig: {native: true}});
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(record.form.tagName, "step-flow-form");
  assert.equal(record.context.tagName, "habab-native-form-context");
  assert.equal(record.form.flowConfig.native, true);
  assert.equal(record.context.hass, card.hass);
});

test("native form button uses native label, delegates submit, and tracks loading", async () => {
  const bridgeCalls = installBridge(() => ({
    tag: "step-flow-form", contextTag: "habab-native-form-context",
    flowConfig: {renderShowFormStepSubmitButton: () => "Next step"},
  }));
  const card = setupShell(new module.HababIntegrationSetup());
  card._hass = {connected: true, language: "en", async callWS() { return {interactions: []}; }};
  card._applyInteractions([formRecord()]);
  await new Promise((resolve) => setImmediate(resolve));

  const record = card.cards.get("i1");
  let finishSubmit;
  let submitCount = 0;
  record.form.submit = () => {
    submitCount += 1;
    return new Promise((resolve) => { finishSubmit = resolve; });
  };
  assert.equal(record.submitButton.textContent, "Next step");
  record.submitButton.dispatchEvent(new Event("click"));
  assert.equal(submitCount, 1);
  assert.equal(record.submitButton.disabled, true);
  record.submitButton.dispatchEvent(new Event("click"));
  assert.equal(submitCount, 1);

  const loading = new CustomEventStub("flow-step-footer-state-changed", {detail: {loading: true}});
  record.form.dispatchEvent(loading);
  assert.equal(loading.cancelBubble, true);
  assert.equal(record.formLoading, true);
  finishSubmit();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(record.submitButton.disabled, true);
  record.form.dispatchEvent(new CustomEventStub("flow-step-footer-state-changed", {detail: {loading: false}}));
  assert.equal(record.submitButton.disabled, false);
  assert.equal(bridgeCalls.length, 1);
});

test("done action is labeled as connection verification", async () => {
  const card = setupShell(new module.HababIntegrationSetup());
  card._hass = {connected: true, language: "ru", async callWS() { return {interactions: []}; }};
  card._applyInteractions([formRecord({phase: "done", entry_state: "loaded"})]);
  assert.equal(card.cards.get("i1").actions.children[0].textContent, "Проверить подключение");
});

test("loading, expiry, and fallback errors use the selected language", async () => {
  installBridge(() => new Promise(() => {}));
  const card = setupShell(new module.HababIntegrationSetup());
  card._hass = {connected: true, language: "ru", localize: (key) => key, async callWS() { return {interactions: []}; }};
  card._applyInteractions([formRecord()]);
  assert.equal(card.cards.get("i1").body.children[0].textContent, "Загружаем форму настройки…");
  card._applyInteractions([formRecord({phase: "expired"})]);
  assert.equal(card.cards.get("i1").body.children[0].textContent, "Время ожидания этого шага истекло. Начните подключение снова из чата.");
  card._applyInteractions([formRecord({phase: "error", errors: {base: "setup_failed"}})]);
  assert.equal(card.cards.get("i1").body.children[0].textContent, "Не удалось настроить интеграцию.");
});

test("menu clicks submit normalized string choices under Home Assistant next_step_id", async () => {
  const calls = [];
  const card = setupShell(new module.HababIntegrationSetup());
  card._hass = {connected: true, language: "en", async callWS(message) {
    calls.push(message);
    return {id: "menu1", domain: "mqtt", kind: "config", phase: "done", entry_state: "loaded"};
  }};
  card._applyInteractions([{
    id: "menu1", domain: "mqtt", kind: "config", phase: "menu",
    menu_options: [{value: "time_date", label: "Configure date and time"}],
  }]);

  const optionButton = card.cards.get("menu1").body.children[0].children[0];
  optionButton.dispatchEvent(new Event("click"));
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(calls[0], {
    type: "habab_home/setup/submit", interaction_id: "menu1",
    input: {next_step_id: "time_date"}, language: "en",
  });
});

test("options flows use the existing config entry id with the narrow native form", async () => {
  const bridgeCalls = installBridge();
  const card = setupShell(new module.HababIntegrationSetup());
  card._hass = {connected: true, language: "en", async callWS() { return {interactions: []}; }};

  card._applyInteractions([formRecord({kind: "options", entry_id: "entry-123"})]);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(bridgeCalls[0].flowType, "options_flow");
  assert.equal(bridgeCalls[0].entryId, "entry-123");
  assert.equal(bridgeCalls[0].handler, "mqtt");
  assert.equal(bridgeCalls[0].step.handler, "entry-123");
  const record = card.cards.get("i1");
  assert.equal(record.form.step.handler, "entry-123");
  assert.equal(record.form.narrow, true);
  assert.equal(record.context.narrow, true);
});

test("same step error refresh updates native step without replacing native form state", async () => {
  installBridge();
  const card = setupShell(new module.HababIntegrationSetup());
  card._hass = {connected: true, language: "en", async callWS() { return {interactions: []}; }};
  card._applyInteractions([formRecord()]);
  await new Promise((resolve) => setImmediate(resolve));
  const current = card.cards.get("i1");
  current.form.secretInput = "secret-value";

  card._applyInteractions([formRecord({errors: {password: "invalid_auth"}})]);
  assert.equal(current.form.secretInput, "secret-value");
  assert.deepEqual(current.form.step.errors, {password: "invalid_auth"});

  card._applyInteractions([formRecord({step_id: "confirm", data_schema: [{name: "code", type: "string"}]})]);
  assert.equal(current.form, undefined);
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(current.form);
});

test("status refresh never reparents a mounted native form", async () => {
  installBridge();
  const card = setupShell(new module.HababIntegrationSetup());
  card._hass = {connected: true, language: "en"};
  let mounts = 0;
  // A real DOM append moves an existing child and runs its disconnect hooks.
  card._cardsRoot.append = (...elements) => {
    mounts += elements.length;
    for (const element of elements) element.parentNode = card._cardsRoot;
  };
  card._applyInteractions([formRecord()]);
  card._render();
  await new Promise((resolve) => setImmediate(resolve));
  const form = card.cards.get("i1").form;
  form.privateDraft = "unsent protected input";
  card._applyInteractions([formRecord({errors: {password: "invalid_auth"}})]);
  card._render();
  card._render();
  assert.equal(mounts, 1, "refresh must not disconnect native editors or their contexts");
  assert.equal(card.cards.get("i1").form, form);
  assert.equal(form.privateDraft, "unsent protected input");
});

test("native submit sends protected values directly to controller and emits no form data", async () => {
  const calls = [];
  const bridgeCalls = installBridge();
  const card = setupShell(new module.HababIntegrationSetup());
  const emitted = [];
  card.addEventListener("habab-setup-state", (event) => emitted.push(event.detail));
  card._hass = {connected: true, language: "ru", async callWS(message) {
    calls.push(message);
    if (message.type === "habab_home/setup/submit") {
      return {id: "i1", domain: "mqtt", kind: "config", phase: "done", entry_state: "loaded", entity_count: 3};
    }
    return {interactions: []};
  }};
  card._applyInteractions([formRecord()]);
  await new Promise((resolve) => setImmediate(resolve));
  const form = card.cards.get("i1").form;
  const privateInput = {password: "secret-value"};
  const nativeStep = await bridgeCalls[0].submit(card.hass, "i1", privateInput);
  assert.equal(nativeStep.type, "create_entry");
  assert.deepEqual(calls, [{
    type: "habab_home/setup/submit", interaction_id: "i1", input: privateInput, language: "ru",
  }]);
  assert.deepEqual(emitted, [{phase: "form", domain: "mqtt"}]);

  const update = new CustomEventStub("flow-update", {bubbles: true, detail: {step: nativeStep}});
  form.dispatchEvent(update);
  assert.equal(update.cancelBubble, true);
  assert.deepEqual(emitted, [{phase: "form", domain: "mqtt"}, {phase: "done", domain: "mqtt"}]);
  assert.equal(JSON.stringify(emitted).includes("secret-value"), false);
});

test("backend errors passed back to native form contain only safe codes", async () => {
  const bridgeCalls = installBridge();
  const card = setupShell(new module.HababIntegrationSetup());
  card._hass = {connected: true, language: "en", async callWS() {
    const error = new Error("provider response contains secret-value");
    error.code = "invalid_auth";
    throw error;
  }};
  card._applyInteractions([formRecord()]);
  await new Promise((resolve) => setImmediate(resolve));
  await assert.rejects(bridgeCalls[0].submit(card.hass, "i1", {password: "secret-value"}), (error) => {
    assert.deepEqual(error.body, {errors: {base: "invalid_auth"}});
    assert.equal(error.message.includes("secret-value"), false);
    return true;
  });
});

test("hass updates avoid status polling except on connection or language changes", async () => {
  const calls = [];
  const card = setupShell(new module.HababIntegrationSetup());
  card.hass = {connected: true, language: "en", async callWS(message) { calls.push(message); return {interactions: []}; }};
  await new Promise((resolve) => setImmediate(resolve));
  card.hass = {connected: true, language: "en", async callWS(message) { calls.push(message); return {interactions: []}; }};
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.length, 1);
  card.hass = {connected: true, language: "ru", async callWS(message) { calls.push(message); return {interactions: []}; }};
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls.map((call) => call.language), ["en", "ru"]);
});

test("late status responses cannot remount cards after disconnect", async () => {
  let resolveStatus;
  const card = setupShell(new module.HababIntegrationSetup());
  card._hass = {connected: true, async callWS() {
    return new Promise((resolve) => { resolveStatus = resolve; });
  }};
  const refresh = card.refresh();
  await new Promise((resolve) => setImmediate(resolve));
  card.isConnected = false;
  card.disconnectedCallback();
  resolveStatus({interactions: [formRecord({phase: "done"})]});
  await refresh;
  assert.equal(card.cards.size, 0);
  assert.equal(card._cardsRoot.children.length, 0);
  assert.equal(card.pollTimer, undefined);
});

test("server defaults are stripped and external URLs reject unsafe protocols", () => {
  const schema = module.stripFormDefaults([{name: "token", default: "secret", selector: {text: {suggested_value: "secret"}}}]);
  assert.deepEqual(schema, [{name: "token", selector: {text: {}}}]);
  assert.equal(module.safeExternalUrl("javascript:alert(1)", "https://ha.local/"), undefined);
  assert.equal(module.safeExternalUrl("https://example.com/oauth", "https://ha.local/"), "https://example.com/oauth");
});

test.after(() => {
  for (const [key, descriptor] of saved) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }
});
