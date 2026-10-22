if (!process.env.HA_DEMO_QA_CDP_URL) throw new Error('Set an explicitly owned HA_DEMO_QA_CDP_URL; shared CDP endpoints must not be used.');
/* Focused one-shot API failure and return-to-demo acceptance (desktop/mobile). */
const puppeteer = require('/root/.nvm/versions/node/v22.21.1/lib/node_modules/puppeteer');
const BASE = process.env.HA_DEMO_QA_BASE || 'http://127.0.0.1:5137';
const CDP = process.env.HA_DEMO_QA_CDP_URL;
const ORIGIN = new URL(BASE).origin;
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const cardSource = `(root,label)=>{let found;function visit(node){for(const el of node.querySelectorAll('*')){const r=el.getBoundingClientRect(),s=getComputedStyle(el);if(!found&&el.tagName==='HA-CARD'&&(el.innerText||'').includes(label)&&r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'&&s.opacity!=='0')found=el;if(el.shadowRoot)visit(el.shadowRoot)}}visit(root);return found}`;
const buttonSource = `(root,label)=>{let found;function visit(node){for(const el of node.querySelectorAll('button')){const r=el.getBoundingClientRect(),s=getComputedStyle(el);if(el.getAttribute('aria-label')===label&&r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'&&s.opacity!=='0')found=el}for(const el of node.querySelectorAll('*'))if(el.shadowRoot)visit(el.shadowRoot)}visit(root);return found}`;

async function waitForCounter(read, expected, label) {
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline) {
    if (read() >= expected) return;
    await wait(100);
  }
  throw new Error(`Timed out waiting for ${label}`);
}

async function pollPage(page, predicate, args, label, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await page.evaluate(predicate, ...args)) return;
    await wait(250);
  }
  throw new Error(`Timed out waiting for ${label}`);
}

async function clickLamp(page) {
  await pollPage(page, (src) => !!eval(src)(document, 'Торшер'), [cardSource], 'Russian lamp tile', 45000);
  // After error recovery, HA can publish the card before its layout settles.
  await new Promise((resolve) => setTimeout(resolve, 3000));
  const cardRect = await page.evaluate((src) => {
    const card = eval(src)(document, 'Торшер');
    if (!card || !card.getBoundingClientRect().width) throw new Error('Visible Russian lamp card missing');
    const rect = card.getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
  }, cardSource);
  await page.mouse.click(cardRect.x, cardRect.y);
  try {
    await pollPage(page, (src) => !!eval(src)(document, 'Переключить'), [buttonSource], 'native light toggle', 15000);
  } catch (error) {
    await page.screenshot({ path: `/tmp/ha-demo-click-fail-${page.viewport().width}.png` });
    const diagnostic = await page.evaluate((src) => ({
      url: location.href,
      failure: document.querySelector('[data-ha-demo-state-failure]')?.innerText || null,
      lamp: eval(src)(document, 'Торшер')?.innerText || null,
      dialogs: [...document.querySelectorAll('ha-more-info-dialog, dialog')].map((el) => el.innerText),
    }), cardSource);
    throw new Error(`Native light detail did not open: ${JSON.stringify(diagnostic)}; ${error.message}`);
  }
  const toggleRect = await page.evaluate((src) => {
    const button = eval(src)(document, 'Переключить');
    if (!button) throw new Error('Visible native light toggle missing');
    const rect = button.getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
  }, buttonSource);
  await page.mouse.click(toggleRect.x, toggleRect.y);
}

async function state(page) {
  return page.evaluate(async () => (await (await fetch('/api/ha-demo/state', { credentials: 'same-origin', cache: 'no-store' })).json()).states['light.living_room_floor_lamp'].state);
}

async function returnToHome(page) {
  await page.waitForSelector('[data-ha-demo-state-failure] button', { timeout: 10000 });
  const text = await page.$eval('[data-ha-demo-state-failure]', (el) => el.innerText);
  const normalized = text.toLocaleLowerCase('ru');
  if (!normalized.includes('не сохранено') && !normalized.includes('не удалось загрузить')) throw new Error(`Failure not explained: ${text}`);
  const label = await page.$eval('[data-ha-demo-state-failure] button', (el) => el.textContent);
  if (label !== 'Вернуться в демо') throw new Error(`Unexpected return action: ${label}`);
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 20000 }),
    page.$eval('[data-ha-demo-state-failure] button', (button) => button.click()),
  ]);
  try {
    await pollPage(page, (src) => !!eval(src)(document, 'Торшер') && !document.querySelector('[data-ha-demo-state-failure]'), [cardSource], 'returned dashboard and room tile', 60000);
  } catch (error) {
    const diagnostic = await page.evaluate((src) => ({
      url: location.href,
      failure: document.querySelector('[data-ha-demo-state-failure]')?.innerText || null,
      lampVisible: !!eval(src)(document, 'Торшер'),
      bodyText: document.body.innerText.slice(0, 500),
    }), cardSource);
    throw new Error(`Demo did not return to dashboard: ${JSON.stringify(diagnostic)}; ${error.message}`);
  }
  const route = await page.evaluate(() => ({ path: location.pathname, search: location.search, hash: location.hash }));
  if (route.search || (route.hash && route.hash !== '#/lovelace/overview')) throw new Error(`Stale demo route survived recovery: ${JSON.stringify(route)}`);
  return route;
}

async function testViewport(browser, viewport) {
  const context = await browser.createBrowserContext();
  try {
    const page = await context.newPage();
    await page.setViewport(viewport);
    await page.evaluateOnNewDocument(() => {
      window.__demoEntityUpdates = [];
      const timer = window.setInterval(() => {
        const entity = document.querySelector('ha-demo')?.hass?.mockEntities?.['light.living_room_floor_lamp'];
        if (!entity || entity.__qaWrapped) return;
        entity.__qaWrapped = true;
        const update = entity.update;
        entity.update = function (changes) {
          window.__demoEntityUpdates.push({ before: this.state, changes, stack: new Error().stack?.split('\n').slice(1, 5) });
          return update.call(this, changes);
        };
        window.clearInterval(timer);
      }, 0);
    });
    let failGet = 1;
    let failPost = 0;
    const api = { gets: 0, posts: 0, failedPosts: 0, savedPosts: 0, payloads: [], readStates: [] };
    const external = [];
    await page.setRequestInterception(true);
    page.on('response', (response) => {
      const url = new URL(response.url());
      if (url.pathname === '/api/ha-demo/state' && response.request().method() === 'POST' && response.status() === 200) api.savedPosts += 1;
      if (url.pathname === '/api/ha-demo/state' && response.request().method() === 'GET' && response.status() === 200) {
        void response.json().then((data) => api.readStates.push(data.states?.['light.living_room_floor_lamp']?.state ?? null)).catch(() => undefined);
      }
    });
    page.on('request', (request) => {
      const url = new URL(request.url());
      if (url.origin !== ORIGIN) { external.push(request.url()); return request.abort('blockedbyclient'); }
      if (url.pathname === '/api/ha-demo/state' && request.method() === 'GET') {
        api.gets += 1;
        if (failGet) { failGet -= 1; return request.respond({ status: 503, contentType: 'application/json', body: '{}' }); }
      }
      if (url.pathname === '/api/ha-demo/state' && request.method() === 'POST') {
        api.posts += 1;
        try { api.payloads.push(JSON.parse(request.postData() || '{}')); } catch { api.payloads.push(null); }
        if (failPost) { failPost -= 1; api.failedPosts += 1; return request.respond({ status: 503, contentType: 'application/json', body: '{}' }); }
      }
      return request.continue();
    });
    await page.goto(`${BASE}/demos/home-assistant/?more-info-entity-id=light.living_room_floor_lamp#/overview`, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForSelector('[data-ha-demo-state-failure][role="alert"]', { timeout: 30000 });
    const startupMessage = await page.$eval('[data-ha-demo-state-failure]', (el) => el.innerText);
    if (!startupMessage.includes('Не удалось загрузить состояние демо')) throw new Error(`${viewport.name}: failed startup state was not explained`);
    const getRoute = await returnToHome(page);
    if (api.posts !== 0) throw new Error(`${viewport.name}: recovery bootstrap unexpectedly POSTed ${api.posts} writes`);
    if (await state(page) !== 'on') throw new Error(`${viewport.name}: GET recovery did not restore server-seeded on state`);
    await clickLamp(page);
    await waitForCounter(() => api.posts, 1, 'native post after GET recovery');
    await waitForCounter(() => api.savedPosts, 1, 'saved native post after GET recovery');
    if (await state(page) !== 'off') throw new Error(`${viewport.name}: native control did not persist after GET recovery`);

    failPost = 1;
    await clickLamp(page);
    await page.waitForSelector('[data-ha-demo-state-failure][role="alert"]', { timeout: 15000 });
    if (!await page.$eval('[data-ha-demo-state-failure]', (el) => el.innerText.includes('Изменение не сохранено'))) {
      throw new Error(`${viewport.name}: failed write was not explicitly marked unsaved`);
    }
    if (await state(page) !== 'off') throw new Error(`${viewport.name}: failed write changed persisted server state`);
    const postRoute = await returnToHome(page);
    if (api.posts !== 2) throw new Error(`${viewport.name}: POST recovery bootstrap changed the write count: ${api.posts}`);
    if (await state(page) !== 'off') throw new Error(`${viewport.name}: POST recovery did not restore last saved state`);
    const restoredDiagnostic = await page.evaluate((src) => {
      const tile = eval(src)(document, 'Торшер');
      const demo = document.querySelector('ha-demo');
      const id = 'light.living_room_floor_lamp';
      return {
        tile: tile?.innerText || '',
        hass: demo?.hass?.states?.[id]?.state || null,
        mockEntity: demo?.hass?.mockEntities?.[id]?.state || null,
        mockEntityState: demo?.hass?.mockEntities?.[id]?.toState?.().state || null,
      };
    }, cardSource);
    if (!restoredDiagnostic.tile.includes('Выключено') || restoredDiagnostic.hass !== 'off' ||
        restoredDiagnostic.mockEntity !== 'off' || restoredDiagnostic.mockEntityState !== 'off') {
      const updates = await page.evaluate(() => window.__demoEntityUpdates || []);
      throw new Error(`${viewport.name}: restored UI/mock/API state mismatch: ${JSON.stringify(restoredDiagnostic)}, reads=${JSON.stringify(api.readStates)}, updates=${JSON.stringify(updates)}`);
    }
    await clickLamp(page);
    await waitForCounter(() => api.posts, 3, 'native post after POST recovery');
    await waitForCounter(() => api.savedPosts, 2, 'saved native post after POST recovery');
    const finalState = await state(page);
    if (finalState !== 'on') throw new Error(`${viewport.name}: native control did not persist after POST recovery (state=${finalState}, API=${JSON.stringify(api)})`);
    if (external.length) throw new Error(`${viewport.name}: external runtime requests: ${external.join(', ')}`);
    return {
      viewport: viewport.name,
      initialGetFailure: '503 shown; no mock controls until recovery',
      postFailure: '503 shown as unsaved; database state unchanged',
      getRoute,
      postRoute,
      finalSavedState: 'on',
      apiGets: api.gets,
      apiPosts: api.posts,
      forcedPostFailures: api.failedPosts,
      externalRequests: external.length,
    };
  } finally {
    await context.close();
  }
}

(async () => {
  const browser = await puppeteer.connect({ browserURL: CDP });
  try {
    const results = [];
    results.push(await testViewport(browser, { name: 'desktop-1280x800', width: 1280, height: 800 }));
    results.push(await testViewport(browser, { name: 'mobile-390x844', width: 390, height: 844, isMobile: true, hasTouch: true }));
    console.log(JSON.stringify(results, null, 2));
  } finally {
    await browser.disconnect();
  }
})().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
