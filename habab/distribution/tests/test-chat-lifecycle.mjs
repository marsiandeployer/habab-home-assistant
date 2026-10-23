import assert from "node:assert/strict";
import test from "node:test";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

test("global chat launcher follows the authenticated main element across routes", async () => {
  const keys = ["HTMLElement", "customElements", "document", "MutationObserver", "setInterval"];
  const descriptors = new Map(keys.map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const intervals = [];
  const definitions = new Map();
  const children = [];
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
  try {
    const chatModule = await import(`${pathToFileURL(resolve("habab/distribution/src/habab_home/frontend/setup-chat.js"))}?lifecycle-test`);
    intervals[0](); // wait for HA's custom element registry, then install the global launcher
    intervals[1]();
    assert.equal(children.length, 1);
    const launcher = children[0];
    assert.equal(launcher.hass, main.hass);
    assert.equal(launcher.language, "ru");
    const history = [
      { role: "user", text: "Connect Xiaomi Home" },
      { role: "assistant", text: "" },
      { role: "assistant", text: "  \n" },
      { role: "assistant", text: "The setup card is ready." },
    ];
    assert.deepEqual(chatModule.visibleTextMessages(history), [history[0], history[3]]);
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
  } finally {
    for (const [key, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});
