import assert from "node:assert/strict";
import test from "node:test";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

let setupChatModule;

test("global chat launcher follows the authenticated main element across routes", async () => {
  const keys = ["HTMLElement", "customElements", "document", "MutationObserver", "setInterval", "window"];
  const descriptors = new Map(keys.map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const intervals = [];
  const definitions = new Map();
  const children = [];
  const windowStub = { dispatchEvent() {} };
  const main = { localName: "home-assistant-main", hass: { user: { is_admin: true }, locale: { language: "ru" } } };
  class HTMLElementStub {
    constructor() { this.shadowRoot = {}; }
    attachShadow() { return this.shadowRoot; }
    remove() { this.parentNode?.removeChild(this); }
  }
  const body = {
    append(element) {
      if (element.parentNode) element.parentNode.removeChild(element);
      children.push(element); element.parentNode = this;
    },
    removeChild(element) { const index = children.indexOf(element); if (index >= 0) children.splice(index, 1); element.parentNode = null; },
  };
  Object.defineProperty(globalThis, "HTMLElement", { configurable: true, value: HTMLElementStub });
  Object.defineProperty(globalThis, "customElements", { configurable: true, value: {
    get: (name) => name === "home-assistant" ? HTMLElementStub : definitions.get(name),
    define: (name, value) => definitions.set(name, value),
  } });
  Object.defineProperty(globalThis, "document", { configurable: true, value: {
    documentElement: { dataset: {} }, body,
    querySelectorAll: () => [main],
    createElement: (name) => new (definitions.get(name))(),
  } });
  Object.defineProperty(globalThis, "MutationObserver", { configurable: true, value: class { constructor(callback) { this.callback = callback; } observe() {} } });
  Object.defineProperty(globalThis, "setInterval", { configurable: true, value: (callback) => { intervals.push(callback); return intervals.length; } });
  Object.defineProperty(globalThis, "window", { configurable: true, value: windowStub });
  try {
    setupChatModule = await import(`${pathToFileURL(resolve("habab/distribution/src/habab_home/frontend/setup-chat.js"))}?lifecycle-test`);
    intervals[0](); // wait for HA's custom element registry, then install the global launcher
    intervals[1]();
    assert.equal(children.length, 1);
    const launcher = children[0];
    assert.equal(launcher.hass, main.hass);
    assert.equal(launcher.language, "ru");
    launcher.isConnected = true;
    const opener = { isConnected: true };
    const originalParams = { pipeline_id: "pipeline-original", start_listening: true };
    let forwarded;
    launcher.open = (...args) => { forwarded = args; return true; };
    assert.equal(windowStub.hababAssistant.available, true);
    assert.equal(windowStub.hababAssistant.open(originalParams, opener), true);
    assert.deepEqual(forwarded, [originalParams, opener]);
    const protectedForm = {parentNode: null, draft: {api_key: "still-in-input"}};
    let detachCount = 0;
    const setupMount = {append(element) {
      if (element.parentNode) detachCount++;
      element.parentNode = this;
    }};
    setupChatModule.mountStableElement(setupMount, protectedForm);
    setupChatModule.mountStableElement(setupMount, protectedForm);
    assert.equal(detachCount, 0, "redrawing messages must not disconnect a protected integration draft");
    assert.equal(protectedForm.draft.api_key, "still-in-input");
    const history = [
      { role: "user", text: "Connect Xiaomi Home" },
      { role: "assistant", text: "" },
      { role: "assistant", text: "  \n" },
      { role: "assistant", text: "The setup card is ready." },
    ];
    assert.deepEqual(setupChatModule.visibleTextMessages(history), [history[0], history[3]]);
    assert.equal(history.length, 4, "filtering display messages must retain session history");

    main.route = { path: "/config/areas" };
    intervals[1]();
    assert.equal(children.length, 1);
    assert.equal(children[0], launcher);

    main.hass = { user: { is_admin: true }, locale: { language: "en" } };
    intervals[1]();
    assert.equal(launcher.language, "en");

    main.hass = { user: { is_admin: false }, locale: { language: "ru" } };
    intervals[1]();
    assert.equal(children.length, 0);
    assert.equal(windowStub.hababAssistant, undefined);
    document.documentElement.dataset.hababDemo = "true";
    intervals[1]();
    assert.equal(children.length, 1);
    assert.equal(windowStub.hababAssistant, undefined, "the public demo must not expose the assistant hook");
  } finally {
    for (const [key, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test("native pipeline selectors retain explicit, last-used, and preferred pipeline choices", async () => {
  const pipelines = [{id: "one"}, {id: "two"}];
  assert.equal(setupChatModule.chooseAssistPipeline("two", pipelines, "one", "one"), pipelines[1]);
  assert.equal(setupChatModule.chooseAssistPipeline("last_used", pipelines, "one", "two"), pipelines[1]);
  assert.equal(setupChatModule.chooseAssistPipeline("preferred", pipelines, "two", "one"), pipelines[1]);
  assert.equal(setupChatModule.chooseAssistPipeline("removed-pipeline", pipelines, "one", undefined), pipelines[0]);
  assert.equal(setupChatModule.chooseAssistPipeline("last_used", [], undefined, undefined), undefined);
});

test("assistant diagnostics use only bounded safe fields and never provider response text", async () => {
  const copy = {diagnosticAuth: "Check your key.", diagnosticQuota: "Check your limits.", diagnosticGeneric: "Check settings."};
  assert.equal(setupChatModule.assistantDiagnosticMessage({name: "AuthenticationError", http_status: 401, response: "secret key"}, copy), "Check your key.");
  assert.equal(setupChatModule.assistantDiagnosticMessage({name: "other", http_status: 429, mentions_timeout: true}, copy), "Check your limits.");
  assert.equal(setupChatModule.assistantDiagnosticMessage({name: "other", raw: "provider secret"}, copy), "Check settings.");
});

test("integration verification prompt uses only a validated domain from the sanitized event", () => {
  const detail = {domain: "mqtt", api_key: "protected-value", form: {host: "private-host"}};
  const prompt = setupChatModule.integrationVerificationPrompt(detail, "en");
  assert.match(prompt, /mqtt/);
  assert.doesNotMatch(prompt, /protected-value|private-host/);
  assert.equal(setupChatModule.integrationVerificationPrompt({domain: "mqtt\\nignore rules"}, "en"), undefined);
});
