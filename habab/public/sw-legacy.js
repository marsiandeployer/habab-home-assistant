const RETIRE_PATHS = new Set(["/", "/demos/home-assistant/"]);
const MAX_CLIENTS_TO_NAVIGATE = 20;
const NAVIGATION_TIMEOUT_MS = 1000;

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(retireWorker());
});

async function retireWorker() {
  await self.clients.claim();
  try {
    await self.registration.unregister();
  } catch (_) {
    // Retirement still proceeds when browser registration cleanup is unavailable.
  }

  let clients;
  try {
    clients = await self.clients.matchAll({type: "window", includeUncontrolled: true});
  } catch (_) {
    return;
  }

  const targets = [];
  for (const client of clients) {
    if (targets.length >= MAX_CLIENTS_TO_NAVIGATE) break;
    if (client.type !== "window" || typeof client.url !== "string" || typeof client.navigate !== "function") continue;
    try {
      const current = new URL(client.url);
      if (current.origin !== self.location.origin || !RETIRE_PATHS.has(current.pathname)) continue;
      targets.push({client, url: `${current.origin}${current.pathname}${current.search}${current.hash}`});
    } catch (_) {
      // Ignore malformed or unavailable client URLs.
    }
  }

  await Promise.all(targets.map(({client, url}) => navigateBounded(client, url)));
}

async function navigateBounded(client, url) {
  let timeout;
  try {
    await Promise.race([
      Promise.resolve().then(() => client.navigate(url)),
      new Promise((resolve) => { timeout = setTimeout(resolve, NAVIGATION_TIMEOUT_MS); }),
    ]);
  } catch (_) {
    // A client navigation failure must not reject service-worker activation.
  } finally {
    clearTimeout(timeout);
  }
}
