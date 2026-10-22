if (!process.env.HA_DEMO_QA_CDP_URL) throw new Error('Set an explicitly owned HA_DEMO_QA_CDP_URL; shared CDP endpoints must not be used.');
/* Verify saved demo state hydrates through the live HA host into native cards. */
const puppeteer = require('/root/.nvm/versions/node/v22.21.1/lib/node_modules/puppeteer');

const BASE = process.env.HA_DEMO_QA_BASE || 'http://127.0.0.1:5137';
const ORIGIN = new URL(BASE).origin;
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const cardSource = `(root,label)=>{let found;function visit(node){for(const el of node.querySelectorAll('*')){const r=el.getBoundingClientRect(),s=getComputedStyle(el);if(!found&&el.tagName==='HA-CARD'&&(el.innerText||'').includes(label)&&r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'&&s.opacity!=='0')found=el;if(el.shadowRoot)visit(el.shadowRoot)}}visit(root);return found}`;

async function poll(page, predicate, label, ...args) {
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline) {
    const result = await page.evaluate(predicate, ...args);
    if (result) return result;
    await wait(150);
  }
  throw new Error(`Timed out waiting for ${label}`);
}

async function checkViewport(browser, viewport) {
  const context = await browser.createBrowserContext();
  try {
    const page = await context.newPage();
    await page.setViewport(viewport);
    let posts = 0;
    const externalRequests = [];
    await page.setRequestInterception(true);
    page.on('request', (request) => {
      const url = new URL(request.url());
      if (url.origin !== ORIGIN) {
        externalRequests.push(request.url());
        return request.abort('blockedbyclient');
      }
      if (url.pathname === '/api/ha-demo/state' && request.method() === 'POST') posts += 1;
      return request.continue();
    });

    await page.goto(`${BASE}/demos/home-assistant/`, { waitUntil: 'domcontentloaded' });
    await poll(page, (source) => {
      const demo = document.querySelector('ha-demo');
      return !!demo?.hass?.states?.['light.living_room_floor_lamp'] &&
        !!eval(source)(document, 'Торшер');
    }, 'initial demo dashboard', cardSource);
    if (posts !== 0) throw new Error(`${viewport.name}: startup unexpectedly POSTed ${posts} times`);

    const seed = await page.evaluate(async () => fetch('/api/ha-demo/state', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entities: [{ entity_id: 'light.living_room_floor_lamp', state: 'off', brightness: 80 }] }),
    }).then(async (response) => ({ status: response.status, body: await response.text() })));
    if (seed.status !== 200) throw new Error(`${viewport.name}: test state write failed (${seed.status})`);
    if (posts !== 1) throw new Error(`${viewport.name}: expected one test write, observed ${posts}`);

    await page.reload({ waitUntil: 'domcontentloaded' });
    const restored = await poll(page, (source) => {
      const id = 'light.living_room_floor_lamp';
      const demo = document.querySelector('ha-demo');
      const entity = demo?.hass?.mockEntities?.[id];
      const tile = eval(source)(document, 'Торшер');
      if (demo?.hass?.states?.[id]?.state !== 'off' || entity?.state !== 'off' ||
          entity?.toState?.().state !== 'off' || !tile?.innerText.includes('Выключено')) return null;
      return {
        hostState: demo.hass.states[id].state,
        mockState: entity.state,
        mockToState: entity.toState().state,
        nativeTile: tile.innerText,
      };
    }, 'saved API state in host, mock entity, and Russian native card', cardSource);

    if (posts !== 1) throw new Error(`${viewport.name}: reload emitted an unexpected POST (${posts} total)`);
    if (externalRequests.length) throw new Error(`${viewport.name}: external runtime requests: ${externalRequests.join(', ')}`);
    return { viewport: viewport.name, restored, postsIncludingTestSeed: posts, externalRequests: externalRequests.length };
  } finally {
    await context.close();
  }
}

(async () => {
  const cdp = process.env.HA_DEMO_QA_CDP_URL;
  const browser = await puppeteer.connect({ browserURL: cdp });
  try {
    const results = [
      await checkViewport(browser, { name: 'desktop-1280x800', width: 1280, height: 800 }),
      await checkViewport(browser, { name: 'mobile-390x844', width: 390, height: 844, isMobile: true, hasTouch: true }),
    ];
    console.log(JSON.stringify(results, null, 2));
  } finally {
    await browser.disconnect();
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
