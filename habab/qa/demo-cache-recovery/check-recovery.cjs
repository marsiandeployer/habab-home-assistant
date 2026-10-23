const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const puppeteer = require('../../node_modules/puppeteer-core');

const publicRoot = path.resolve(__dirname, '../../public');
const tombstone = path.resolve(__dirname, '../../build/retire-demo-service-worker.js');
const report = {cases: [], limitation: 'Synthetic stale native shell; actual demo compiler assets and real browser service-worker lifecycle. No private HA, account or hardware access.'};
let phase = 'seed';
const oldHtml = '<!doctype html><title>Stale native Home Assistant</title><p>Unable to connect</p><script>fetch("/auth/token",{method:"POST"});new WebSocket((location.protocol==="https:"?"wss:":"ws:")+"//"+location.host+"/api/websocket");</script>';
const seed = `self.addEventListener('install',e=>e.waitUntil((async()=>{const c=await caches.open('habab-qa-stale-shell');await c.put('/',new Response(${JSON.stringify(oldHtml)},{headers:{'Content-Type':'text/html'}}));await self.skipWaiting();})()));self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));self.addEventListener('fetch',e=>{if(e.request.mode==='navigate')e.respondWith(caches.open('habab-qa-stale-shell').then(c=>c.match('/')));});`;

const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost');
  response.setHeader('Cache-Control', 'no-store');
  if (['/sw-modern.js', '/sw-legacy.js', '/service_worker.js'].includes(url.pathname)) {
    response.setHeader('Content-Type', 'application/javascript');
    response.end(phase === 'seed' ? seed : fs.readFileSync(tombstone));
    return;
  }
  if (url.pathname === '/_qa/start') {
    response.setHeader('Content-Type', 'text/html');
    response.end('<!doctype html><title>Recovery QA seed</title>');
    return;
  }
  if (url.pathname === '/api/ha-demo/state') {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({states: {
      'light.living_room_floor_lamp': {state: 'on'},
      'light.living_room_spotlights': {state: 'off'},
      'climate.living_room': {state: 'heat'},
    }}));
    return;
  }
  if (url.pathname === '/api/ha-demo/chat') {
    response.setHeader('Content-Type', 'application/json');
    response.end('{"messages":[]}');
    return;
  }
  let relative = url.pathname.replace(/^\/demos\/home-assistant\//, '/');
  if (relative === '/') relative = '/index.html';
  const filename = path.resolve(publicRoot, '.' + relative);
  if (!filename.startsWith(publicRoot + path.sep)) { response.writeHead(403).end(); return; }
  try {
    if (!fs.statSync(filename).isFile()) {response.writeHead(404).end(); return;}
    const types = {'.html':'text/html', '.js':'application/javascript', '.json':'application/json', '.svg':'image/svg+xml', '.css':'text/css', '.woff2':'font/woff2', '.png':'image/png', '.jpg':'image/jpeg'};
    response.setHeader('Content-Type', types[path.extname(filename)] || 'application/octet-stream');
    fs.createReadStream(filename).pipe(response);
  } catch (_) {response.writeHead(404).end();}
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await puppeteer.connect({browserURL: 'http://127.0.0.1:9222'});
  const initial = (await browser.pages()).map(page => page.target()._targetId);
  let context;
  try {
    for (const script of ['/sw-modern.js', '/sw-legacy.js', '/service_worker.js']) {
      phase = 'seed';
      context = await browser.createBrowserContext();
      const page = await context.newPage();
      await page.setViewport({width: 1280, height: 800});
      const row = {script, native_auth_requests: 0, native_ws_requests: 0};
      page.on('request', request => {if (request.url().endsWith('/auth/token')) row.native_auth_requests += 1;});
      page.on('websocket', socket => {if (socket.url().endsWith('/api/websocket')) row.native_ws_requests += 1;});
      const cdp = await page.createCDPSession();
      await cdp.send('Network.enable');
      cdp.on('Network.webSocketCreated', event => {if (event.url.endsWith('/api/websocket')) row.native_ws_requests += 1;});
      await page.goto(origin + '/_qa/start');
      await page.evaluate(async script => {
        localStorage.setItem('qa-preserve-auth', 'synthetic-auth-sentinel');
        document.cookie = 'qa-preserve-cookie=synthetic-cookie;path=/';
        await (await caches.open('qa-unrelated-cache')).put('/qa-keep', new Response('keep'));
        await navigator.serviceWorker.register(script, {scope: '/'});
        await navigator.serviceWorker.ready;
      }, script);
      await page.waitForFunction(() => !!navigator.serviceWorker.controller);
      await page.goto(origin + '/?habab-preview=onboarding', {waitUntil: 'networkidle2'});
      assert.equal(await page.title(), 'Stale native Home Assistant');
      assert.ok(row.native_auth_requests > 0);
      assert.ok(row.native_ws_requests > 0);
      row.stale_shell_reproduced = true;
      phase = 'retired';
      await page.evaluate(async () => {const registration = await navigator.serviceWorker.getRegistration(); await registration.update();});
      await page.waitForFunction(() => {
        const host = document.querySelector('habab-setup-chat');
        return host?.preview && host.sessionId && host.shadowRoot.querySelector('dialog')?.open && host.previewState?.status === 'ready';
      }, {timeout: 45000});
      row.recovered = await page.evaluate(async () => {
        const root = document.querySelector('habab-setup-chat').shadowRoot;
        return {
          registrations: (await navigator.serviceWorker.getRegistrations()).length,
          auth_preserved: localStorage.getItem('qa-preserve-auth') === 'synthetic-auth-sentinel',
          cookie_preserved: document.cookie.includes('qa-preserve-cookie=synthetic-cookie'),
          unrelated_cache_preserved: await (await caches.open('qa-unrelated-cache')).match('/qa-keep').then(Boolean),
          stale_cache_preserved: (await caches.keys()).includes('habab-qa-stale-shell'),
          preview_open: root.querySelector('dialog').open,
          dashboard_rows: root.querySelectorAll('.preview-row').length,
        };
      });
      assert.equal(row.recovered.registrations, 0);
      for (const key of ['auth_preserved', 'cookie_preserved', 'unrelated_cache_preserved', 'stale_cache_preserved', 'preview_open']) assert.equal(row.recovered[key], true);
      assert.equal(row.recovered.dashboard_rows, 3);
      const previous = row.native_auth_requests;
      await page.reload({waitUntil: 'networkidle2'});
      await page.waitForFunction(() => document.querySelector('habab-setup-chat')?.sessionId);
      assert.equal(row.native_auth_requests, previous, 'new page must not attempt native authentication');
      row.no_native_auth_after_reload = true;
      if (script === '/sw-modern.js') await page.screenshot({path: path.join(__dirname, 'recovered-desktop.png')});
      report.cases.push(row);
      await context.close();
      context = undefined;
    }
    report.verdict = 'PASS';
  } finally {
    if (context) await context.close();
    report.existing_pages_preserved = (await browser.pages()).filter(page => initial.includes(page.target()._targetId)).length === initial.length;
    await browser.disconnect();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    report.server_closed = true;
    fs.writeFileSync(path.join(__dirname, 'result.json'), JSON.stringify(report, null, 2) + '\n');
  }
  process.stdout.write(JSON.stringify(report) + '\n');
})().catch(error => {process.stderr.write(error.stack + '\n'); server.closeAllConnections(); server.close(); process.exitCode = 1;});
