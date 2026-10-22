if (!process.env.HA_DEMO_QA_CDP_URL) throw new Error('Set an explicitly owned HA_DEMO_QA_CDP_URL; shared CDP endpoints must not be used.');
/*
 * Runtime acceptance for the compiled Home Assistant demo. Connects to the
 * dedicated local CDP endpoint, uses isolated browser contexts, blocks every
 * origin except the local QA server, and closes all contexts on completion.
 */
const fs = require('node:fs');
const path = require('node:path');
const puppeteer = require('/root/.nvm/versions/node/v22.21.1/lib/node_modules/puppeteer');

const BASE = process.env.HA_DEMO_QA_BASE || 'http://127.0.0.1:5137';
const OUT = path.resolve(__dirname, 'evidence');
const LOCAL_ORIGIN = new URL(BASE).origin;
fs.mkdirSync(OUT, { recursive: true });

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const deepText = (root = document) => {
  let text = root.body?.innerText || '';
  const visit = (node) => {
    for (const el of node.querySelectorAll('*')) {
      text += `\n${el.innerText || el.textContent || ''}`;
      if (el.shadowRoot) {
        visit(el.shadowRoot);
      }
    }
  };
  visit(root);
  return text;
};
const findVisibleCard = (root, label) => {
  let found;
  const visit = (node) => {
    for (const el of node.querySelectorAll('ha-card')) {
      if ((el.innerText || '').includes(label)) found = el;
    }
    for (const el of node.querySelectorAll('*')) if (el.shadowRoot) visit(el.shadowRoot);
  };
  visit(root);
  return found;
};
const findVisibleButton = (root, ariaLabel) => {
  let found;
  const visit = (node) => {
    for (const el of node.querySelectorAll('button')) {
      if (el.getAttribute('aria-label') === ariaLabel && el.getBoundingClientRect().width > 0) found = el;
    }
    for (const el of node.querySelectorAll('*')) if (el.shadowRoot) visit(el.shadowRoot);
  };
  visit(root);
  return found;
};

async function setupPage(context, viewport, { failGetCount = 0, failPostCount = 0 } = {}) {
  const page = await context.newPage();
  await page.setViewport(viewport);
  const externalRequests = [];
  const localResponses = [];
  const api = { gets: 0, posts: 0, payloads: [] };
  await page.setRequestInterception(true);
  page.on('response', (response) => {
    const url = new URL(response.url());
    if (url.origin === LOCAL_ORIGIN && response.status() >= 400) {
      localResponses.push({ path: url.pathname, status: response.status() });
    }
  });
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.origin !== LOCAL_ORIGIN) {
      externalRequests.push(request.url());
      return request.abort('blockedbyclient');
    }
    if (url.pathname === '/api/ha-demo/state' && request.method() === 'GET') {
      api.gets += 1;
      if (failGetCount > 0) {
        failGetCount -= 1;
        return request.respond({ status: 503, contentType: 'application/json', body: '{}' });
      }
    }
    if (url.pathname === '/api/ha-demo/state' && request.method() === 'POST') {
      api.posts += 1;
      try { api.payloads.push(JSON.parse(request.postData() || '{}')); } catch { api.payloads.push(null); }
      if (failPostCount > 0) {
        failPostCount -= 1;
        return request.respond({ status: 503, contentType: 'application/json', body: '{}' });
      }
    }
    return request.continue();
  });
  page.on('console', (message) => {
    if (message.type() === 'error') console.error(`console: ${message.text()}`);
  });
  return { page, api, externalRequests, localResponses };
}

async function waitForDemo(page) {
  await page.waitForFunction(() => {
    const allText = () => {
      let text = document.body.innerText || '';
      const visit = (node) => {
        for (const el of node.querySelectorAll('*')) {
          text += `\n${el.innerText || el.textContent || ''}`;
          if (el.shadowRoot) visit(el.shadowRoot);
        }
      };
      visit(document);
      return text;
    };
    return allText().includes('Торшер') || !!document.querySelector('[data-ha-demo-state-failure]');
  }, { timeout: 30000 });
}

async function clickVisibleLight(page) {
  await page.waitForFunction((findCardSource) => {
    const findCard = eval(`(${findCardSource})`);
    return !!findCard(document, 'Торшер');
  }, { timeout: 15000 }, findVisibleCard.toString());
  const cardHandle = await page.evaluateHandle((findCardSource) => {
    const findCard = eval(`(${findCardSource})`);
    return findCard(document, 'Торшер');
  }, findVisibleCard.toString());
  const card = cardHandle.asElement();
  if (!card) throw new Error('Russian lamp card not found');
  await card.click();
  await cardHandle.dispose();
  await clickNativeToggle(page);
}

async function clickNativeToggle(page) {
  await page.waitForFunction((findButtonSource) => {
    const findButton = eval(`(${findButtonSource})`);
    return !!findButton(document, 'Переключить');
  }, { timeout: 10000 }, findVisibleButton.toString());
  const toggleHandle = await page.evaluateHandle((findButtonSource) => {
    const findButton = eval(`(${findButtonSource})`);
    return findButton(document, 'Переключить');
  }, findVisibleButton.toString());
  const toggle = toggleHandle.asElement();
  if (!toggle) throw new Error('Native light toggle button not found');
  await toggle.click();
  await toggleHandle.dispose();
}

async function returnToDemo(page) {
  await page.waitForSelector('[data-ha-demo-state-failure] button', { timeout: 10000 });
  const button = await page.$('[data-ha-demo-state-failure] button');
  if (!button) throw new Error('Demo recovery button is missing');
  const label = await button.evaluate((el) => el.textContent);
  if (label !== 'Вернуться в демо') throw new Error(`Unexpected recovery action label: ${label}`);
  await button.click();
  await waitForDemo(page);
  await page.waitForFunction(() => !document.querySelector('[data-ha-demo-state-failure]'), { timeout: 20000 });
  await delay(700);
  const route = await page.evaluate(() => ({ path: location.pathname, search: location.search, hash: location.hash }));
  if (route.search || (route.hash && route.hash !== '#/lovelace/overview')) {
    throw new Error(`Recovery retained stale route: ${JSON.stringify(route)}`);
  }
  return route;
}

async function main() {
  const browser = await puppeteer.connect({ browserURL: process.env.HA_DEMO_QA_CDP_URL });
  const contexts = [];
  const report = { base: BASE, screenshots: [], checks: {}, externalRequests: [] };
  try {
    // This page is the same-origin parent harness used to verify the actual
    // iframe postMessage contract, including event.origin and source checks.
    const primary = await browser.createBrowserContext();
    contexts.push(primary);
    const { page, api, externalRequests } = await setupPage(primary, { width: 1280, height: 800 });
    await page.goto(`${BASE}/qa-parent/`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForSelector('#ha-demo', { timeout: 10000 });
    await page.waitForFunction(() => {
      const iframe = document.querySelector('#ha-demo');
      return !!iframe?.contentDocument?.querySelector('ha-demo');
    }, { timeout: 30000 });
    const demo = page.frames().find((item) => item.url().includes('/demos/home-assistant/'));
    if (!demo) throw new Error('HA demo iframe did not load');
    await waitForDemo(demo);
    await delay(600);
    const initial = await demo.evaluate(deepText);
    for (const label of [
      'Торшер', 'Термостат', 'Гостиная', 'Кабинет',
      'Включено', 'Обогрев', 'Открыто', 'Заблокировано', 'Переменная облачность',
    ]) {
      if (!initial.includes(label)) throw new Error(`Russian UI label missing: ${label}`);
    }
    if (api.posts !== 0) throw new Error(`Fresh load unexpectedly posted ${api.posts} state writes`);
    if (api.gets !== 1) throw new Error(`Expected one startup GET, got ${api.gets}`);
    await page.screenshot({ path: path.join(OUT, 'desktop-1280x800.png') });
    report.screenshots.push('desktop-1280x800.png');

    await clickVisibleLight(demo);
    if (page.url() !== `${BASE}/qa-parent/`) {
      throw new Error(`The demo changed the containing product URL: ${page.url()}`);
    }
    const colorButton = await demo.evaluateHandle((findButtonSource) => {
      const findButton = eval(`(${findButtonSource})`);
      return findButton(document, 'Цвет');
    }, findVisibleButton.toString());
    const colorControl = colorButton.asElement();
    if (!colorControl) throw new Error('Native color control is not available');
    await colorControl.click();
    await colorButton.dispose();
    await delay(600);
    await delay(500);
    await page.waitForFunction(() => Number(document.querySelector('#interaction-count')?.textContent || 0) > 0,
      { timeout: 10000 });
    await delay(300);
    let state = await demo.evaluate(async () => (await (await fetch('/api/ha-demo/state')).json()).states);
    if (state['light.living_room_floor_lamp']?.state !== 'off') {
      throw new Error(`Native light toggle did not persist off: ${JSON.stringify(state['light.living_room_floor_lamp'])}`);
    }
    const parentInteractions = Number(await page.$eval('#interaction-count', (el) => el.textContent));
    if (parentInteractions < 1) throw new Error('Iframe parent did not receive demo interaction message');
    const colorAsset = await demo.evaluate(async () => {
      const response = await fetch('/demos/home-assistant/static/images/color_wheel.png');
      return { status: response.status, type: response.headers.get('content-type') };
    });
    if (colorAsset.status !== 200) throw new Error(`Native color-wheel asset returned ${colorAsset.status}`);

    // Two additional rapid toggles exercise FIFO persistence. Across three
    // total clicks from the seeded on state, the final native state is off.
    for (let i = 0; i < 2; i++) {
      await clickNativeToggle(demo);
      await delay(150);
    }
    await delay(1200);
    state = await demo.evaluate(async () => (await (await fetch('/api/ha-demo/state')).json()).states);
    if (state['light.living_room_floor_lamp']?.state !== 'off') {
      throw new Error(`Rapid final state was not the last native UI state: ${state['light.living_room_floor_lamp']?.state}`);
    }
    if (api.posts < 3) throw new Error(`Expected three user writes and zero startup writes; got ${api.posts} writes`);
    report.checks.parentInteractionCount = parentInteractions;
    report.checks.nativeLightStateAfterRapidClicks = state['light.living_room_floor_lamp'].state;
    report.checks.nativePostCount = api.posts;
    report.checks.colorWheel = colorAsset;
    report.checks.iframeHistoryIsolation = 'parent URL unchanged';

    await demo.goto(`${BASE}/demos/home-assistant/`, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await waitForDemo(demo);
    await delay(500);
    const afterReload = await demo.evaluate(async () => (await (await fetch('/api/ha-demo/state')).json()).states);
    if (afterReload['light.living_room_floor_lamp']?.state !== 'off') throw new Error('Same-session reload lost saved lamp state');
    report.checks.sameSessionReload = 'off';

    const isolated = await browser.createBrowserContext();
    contexts.push(isolated);
    const isolatedSetup = await setupPage(isolated, { width: 1280, height: 800 });
    await isolatedSetup.page.goto(`${BASE}/demos/home-assistant/`, { waitUntil: 'domcontentloaded' });
    await waitForDemo(isolatedSetup.page);
    await delay(400);
    const isolatedState = await isolatedSetup.page.evaluate(async () => (await (await fetch('/api/ha-demo/state')).json()).states);
    if (isolatedState['light.living_room_floor_lamp']?.state !== 'on') throw new Error('Anonymous browser state leaked between contexts');
    if (isolatedSetup.api.posts !== 0) throw new Error('New anonymous session wrote state during initialization');
    report.checks.newSessionSeed = isolatedState['light.living_room_floor_lamp'].state;

    const mobile = await browser.createBrowserContext();
    contexts.push(mobile);
    const mobileSetup = await setupPage(mobile, { width: 390, height: 844, isMobile: true, hasTouch: true });
    await mobileSetup.page.goto(`${BASE}/demos/home-assistant/`, { waitUntil: 'domcontentloaded' });
    await waitForDemo(mobileSetup.page);
    await delay(500);
    const mobileText = await mobileSetup.page.evaluate(deepText);
    if (!mobileText.includes('Демо-пользователь')) throw new Error('Mobile user label was not localized');
    await mobileSetup.page.screenshot({ path: path.join(OUT, 'mobile-390x844.png'), fullPage: true });
    report.screenshots.push('mobile-390x844.png');
    const mobileSize = await mobileSetup.page.evaluate(() => ({ width: document.documentElement.clientWidth, scrollHeight: document.documentElement.scrollHeight }));
    if (mobileSize.width !== 390 || mobileSize.scrollHeight <= 844) throw new Error(`Unexpected mobile layout: ${JSON.stringify(mobileSize)}`);
    report.checks.mobile = mobileSize;

    const recovery = {};
    for (const viewport of [
      { name: 'desktop', width: 1280, height: 800 },
      { name: 'mobile', width: 390, height: 844, isMobile: true, hasTouch: true },
    ]) {
      const getContext = await browser.createBrowserContext();
      contexts.push(getContext);
      const getSetup = await setupPage(getContext, viewport, { failGetCount: 1 });
      const staleUrl = `${BASE}/demos/home-assistant/?more-info-entity-id=light.living_room_floor_lamp#/overview`;
      await getSetup.page.goto(staleUrl, { waitUntil: 'domcontentloaded' });
      await waitForDemo(getSetup.page);
      await getSetup.page.waitForSelector('[data-ha-demo-state-failure][role="alert"]', { timeout: 10000 });
      const failureText = await getSetup.page.evaluate((getTextSource) => eval(`(${getTextSource})`)(document), deepText.toString());
      if (!failureText.includes('Демо временно недоступно')) throw new Error(`${viewport.name}: GET failure screen is not visible`);
      if (!failureText.includes('Не удалось загрузить состояние демо')) throw new Error(`${viewport.name}: GET failure does not explain state was not loaded`);
      if (await getSetup.page.evaluate((findCardSource) => !!eval(`(${findCardSource})`)(document, 'Торшер'), findVisibleCard.toString())) {
        throw new Error(`${viewport.name}: mock controls remained visible after initial GET failure`);
      }
      const getRoute = await returnToDemo(getSetup.page);
      await clickVisibleLight(getSetup.page);
      await delay(500);
      const getRecoveredState = await getSetup.page.evaluate(async () => (await (await fetch('/api/ha-demo/state')).json()).states);
      if (getRecoveredState['light.living_room_floor_lamp']?.state !== 'off') {
        throw new Error(`${viewport.name}: native control did not save after GET recovery`);
      }
      if (getSetup.api.gets < 3 || getSetup.api.posts !== 1) {
        throw new Error(`${viewport.name}: unexpected GET recovery request counts ${JSON.stringify(getSetup.api)}`);
      }

      const postContext = await browser.createBrowserContext();
      contexts.push(postContext);
      const postSetup = await setupPage(postContext, viewport, { failPostCount: 1 });
      await postSetup.page.goto(`${BASE}/demos/home-assistant/`, { waitUntil: 'domcontentloaded' });
      await waitForDemo(postSetup.page);
      await postSetup.page.evaluate(() => history.replaceState({}, '', `${location.pathname}?more-info-entity-id=light.living_room_floor_lamp#/overview`));
      await clickVisibleLight(postSetup.page);
      await postSetup.page.waitForSelector('[data-ha-demo-state-failure][role="alert"]', { timeout: 10000 });
      const postFailureText = await postSetup.page.evaluate((getTextSource) => eval(`(${getTextSource})`)(document), deepText.toString());
      if (!postFailureText.includes('Изменение не сохранено')) throw new Error(`${viewport.name}: failed POST was not clearly marked unsaved`);
      const retainedServerState = await postSetup.page.evaluate(async () => (await (await fetch('/api/ha-demo/state')).json()).states);
      if (retainedServerState['light.living_room_floor_lamp']?.state !== 'on') {
        throw new Error(`${viewport.name}: failed POST changed server state`);
      }
      const postRoute = await returnToDemo(postSetup.page);
      await clickVisibleLight(postSetup.page);
      await delay(500);
      const savedAfterRetry = await postSetup.page.evaluate(async () => (await (await fetch('/api/ha-demo/state')).json()).states);
      if (savedAfterRetry['light.living_room_floor_lamp']?.state !== 'off') {
        throw new Error(`${viewport.name}: native control did not save after POST recovery`);
      }
      if (postSetup.api.posts !== 2 || postSetup.api.gets < 3) {
        throw new Error(`${viewport.name}: unexpected POST recovery request counts ${JSON.stringify(postSetup.api)}`);
      }
      recovery[viewport.name] = {
        initialGetError: '503 shown; controls blocked',
        postError: '503 clearly reported unsaved; server state unchanged',
        getRecoveryRoute: getRoute,
        postRecoveryRoute: postRoute,
        finalSavedState: savedAfterRetry['light.living_room_floor_lamp'].state,
        retryGetCount: getSetup.api.gets,
        retryPostCount: postSetup.api.posts,
      };
      report.externalRequests.push(...getSetup.externalRequests, ...postSetup.externalRequests);
    }
    report.checks.failureRecovery = recovery;

    for (const item of [externalRequests, isolatedSetup.externalRequests, mobileSetup.externalRequests]) report.externalRequests.push(...item);
    if (report.externalRequests.length) throw new Error(`Unexpected external request attempts: ${report.externalRequests.join(', ')}`);
    report.checks.externalRuntimeRequests = 0;
    fs.writeFileSync(path.join(OUT, 'runtime-report.json'), `${JSON.stringify(report, null, 2)}\n`);
    console.log(JSON.stringify(report, null, 2));
  } finally {
    for (const context of contexts.reverse()) await context.close().catch(() => undefined);
    await browser.disconnect();
  }
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
