import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("./retire-demo-service-worker.js", import.meta.url), "utf8");

function runtime(clients) {
  const listeners = new Map();
  const calls = [];
  const self = {
    location: {origin: "https://habab-demo.wpmix.net"},
    addEventListener(type, callback) {
      listeners.set(type, callback);
    },
    skipWaiting: async () => { calls.push("skipWaiting"); },
    clients: {
      claim: async () => { calls.push("claim"); },
      matchAll: async (options) => { calls.push(["matchAll", options]); return clients; },
    },
    registration: {
      unregister: async () => { calls.push("unregister"); return true; },
    },
  };
  Object.defineProperty(self, "caches", {get() { throw new Error("cache storage must remain untouched"); }});
  const context = {
    self,
    URL,
    Promise,
    setTimeout,
    clearTimeout,
    fetch() { throw new Error("retirement worker must not install a fetch handler"); },
  };
  vm.runInNewContext(source, context, {filename: "retire-demo-service-worker.js"});
  return {listeners, calls};
}

test("install skips waiting and activation retires only public root clients", async () => {
  const destinations = [];
  const client = (url, type = "window", behavior = async (value) => destinations.push(value)) => ({
    url, type, navigate: behavior,
  });
  const targets = [
    client("https://habab-demo.wpmix.net/?habab-preview=onboarding#chat"),
    client("https://habab-demo.wpmix.net/demos/home-assistant/?homescreen=1#climate"),
  ];
  const ignored = [
    client("https://habab-demo.wpmix.net/owner-test?token=keep"),
    client("https://habab-demo.wpmix.net/unknown/path"),
    client("https://other.example/"),
    client("https://habab-demo.wpmix.net/", "worker"),
    {url: "not a URL", type: "window", navigate: async () => destinations.push("malformed")},
  ];
  const {listeners, calls} = runtime([...targets, ...ignored]);
  assert.deepEqual([...listeners.keys()].sort(), ["activate", "install"]);

  let installPromise;
  listeners.get("install")({waitUntil(promise) { installPromise = promise; }});
  await installPromise;
  assert.deepEqual(calls, ["skipWaiting"]);

  let activationPromise;
  listeners.get("activate")({waitUntil(promise) { activationPromise = promise; }});
  await activationPromise;
  assert.deepEqual(calls.slice(1, 3), ["claim", "unregister"]);
  assert.equal(calls[3][0], "matchAll");
  assert.equal(calls[3][1].type, "window");
  assert.equal(calls[3][1].includeUncontrolled, true);
  assert.deepEqual(destinations, [
    "https://habab-demo.wpmix.net/?habab-preview=onboarding#chat",
    "https://habab-demo.wpmix.net/demos/home-assistant/?homescreen=1#climate",
  ]);
});

test("navigation failures and stalled clients are bounded and do not fail activation", async () => {
  const clients = [
    {type: "window", url: "https://habab-demo.wpmix.net/", navigate: async () => { throw new Error("navigation denied"); }},
    {type: "window", url: "https://habab-demo.wpmix.net/demos/home-assistant/", navigate: () => new Promise(() => {})},
  ];
  const {listeners} = runtime(clients);
  let activationPromise;
  listeners.get("activate")({waitUntil(promise) { activationPromise = promise; }});
  await Promise.race([
    activationPromise,
    new Promise((_, reject) => setTimeout(() => reject(new Error("activation exceeded navigation bound")), 1200)),
  ]);
});

test("retirement work is capped even when many public windows are open", async () => {
  let navigations = 0;
  const clients = Array.from({length: 30}, () => ({
    type: "window", url: "https://habab-demo.wpmix.net/", navigate: async () => { navigations++; },
  }));
  const {listeners} = runtime(clients);
  let activationPromise;
  listeners.get("activate")({waitUntil(promise) { activationPromise = promise; }});
  await activationPromise;
  assert.equal(navigations, 20);
});

test("all three public root worker endpoints are generated from the authored tombstone", async () => {
  for (const filename of ["sw-modern.js", "sw-legacy.js", "service_worker.js"]) {
    const published = await readFile(new URL(`../public/${filename}`, import.meta.url), "utf8");
    assert.equal(published, source, `${filename} must stay byte-identical to the authored retirement worker`);
  }
});
