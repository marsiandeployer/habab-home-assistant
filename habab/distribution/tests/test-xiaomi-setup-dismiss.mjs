import assert from "node:assert/strict";
import test from "node:test";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

test("Xiaomi dismiss event is composed for completed or failed setup, not active setup", async () => {
  const keys = ["HTMLElement", "customElements", "CustomEvent"];
  const descriptors = new Map(keys.map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const definitions = new Map();
  const dispatched = [];
  class HTMLElementStub {
    dispatchEvent(event) { dispatched.push(event); return true; }
    attachShadow() { return {}; }
  }
  class CustomEventStub {
    constructor(type, options = {}) { this.type = type; Object.assign(this, options); }
  }

  Object.defineProperty(globalThis, "HTMLElement", { configurable: true, value: HTMLElementStub });
  Object.defineProperty(globalThis, "CustomEvent", { configurable: true, value: CustomEventStub });
  Object.defineProperty(globalThis, "customElements", {
    configurable: true,
    value: {
      get: (name) => definitions.get(name),
      define: (name, value) => definitions.set(name, value),
    },
  });

  try {
    await import(`${pathToFileURL(resolve("habab/distribution/src/habab_home/frontend/xiaomi-setup.js"))}?dismiss-test`);
    const XiaomiSetup = definitions.get("habab-xiaomi-setup");
    const setup = new XiaomiSetup();
    setup.state = { phase: "completed" };
    setup.dismissCompleted();
    assert.equal(dispatched.length, 1);
    assert.equal(dispatched[0].type, "habab-setup-dismiss");
    assert.equal(dispatched[0].bubbles, true);
    assert.equal(dispatched[0].composed, true);

    setup.state = { phase: "error" };
    setup.dismissCompleted();
    assert.equal(dispatched.length, 2, "a failed stale setup can be hidden and reopened");

    setup.state = { phase: "oauth" };
    setup.dismissCompleted();
    assert.equal(dispatched.length, 2, "pending authorization cannot be dismissed");
    setup.state = { phase: "configuring" };
    setup.dismissCompleted();
    assert.equal(dispatched.length, 2, "an in-progress setup cannot be dismissed");
  } finally {
    for (const key of keys) {
      const descriptor = descriptors.get(key);
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test("Xiaomi status reload waits for a newer snapshot when one is already pending", async () => {
  const keys = ["HTMLElement", "customElements", "CustomEvent"];
  const descriptors = new Map(keys.map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  const definitions = new Map();
  class HTMLElementStub {
    dispatchEvent() { return true; }
    attachShadow() { return {}; }
  }
  class CustomEventStub {
    constructor(type, options = {}) { this.type = type; Object.assign(this, options); }
  }
  Object.defineProperty(globalThis, "HTMLElement", {configurable: true, value: HTMLElementStub});
  Object.defineProperty(globalThis, "CustomEvent", {configurable: true, value: CustomEventStub});
  Object.defineProperty(globalThis, "customElements", {configurable: true, value: {
    get: (name) => definitions.get(name), define: (name, value) => definitions.set(name, value),
  }});
  try {
    await import(`${pathToFileURL(resolve("habab/distribution/src/habab_home/frontend/xiaomi-setup.js"))}?load-test`);
    const setup = new (definitions.get("habab-xiaomi-setup"))();
    let resolveFirst;
    let reads = 0;
    setup.render = () => {};
    setup._hass = {async callWS() {
      reads++;
      if (reads === 1) return new Promise((resolve) => { resolveFirst = resolve; });
      return {phase: "idle", setup_id: "current-after-refresh"};
    }};
    const initialLoad = setup.load();
    const newChatLoad = setup.load();
    resolveFirst({phase: "oauth", setup_id: "previous-chat-setup"});
    await Promise.all([initialLoad, newChatLoad]);
    assert.equal(reads, 2);
    assert.equal(setup.state.setup_id, "current-after-refresh");
  } finally {
    for (const key of keys) {
      const descriptor = descriptors.get(key);
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});
