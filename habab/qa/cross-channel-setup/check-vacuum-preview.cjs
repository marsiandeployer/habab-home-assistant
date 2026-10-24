// Read-only check of actual Home favorites; this no longer fabricates vacuum cards.
const fs = require('node:fs');
const puppeteer = require('../../node_modules/puppeteer-core');
const { withOwnedChromium } = require('/root/tools/chrome-daemon/owned-browser.cjs');
const local = process.env.HABAB_VACUUM_QA_MODE === 'local';
const target = process.env.HABAB_VACUUM_QA_URL || (local ? 'http://127.0.0.1:8403' : 'http://127.0.0.1:8123');
const output = process.env.HABAB_VACUUM_QA_OUTPUT || `${__dirname}/${local ? 'local' : 'windows'}-vacuum-preview`;
const report = { recorded_at_utc: new Date().toISOString(), uses_synthetic_fixture: false, physical_motion_verified: false,
  scope: 'Actual authenticated Home favorites and native vacuum controls; no synthetic device or service calls',
  backend_configuration_writes: 0 };
const check = async (page, run) => {
  report.page_errors = [];
  page.on('pageerror', (e) => report.page_errors.push(e.name + ': ' + e.message.slice(0, 160)));
  await run(() => page.setViewport({ width: Number(process.env.HABAB_VACUUM_QA_WIDTH || 1280), height: Number(process.env.HABAB_VACUUM_QA_HEIGHT || 800) }));
  if (local && process.env.HABAB_QA_AUTH_FILE) {
    const auth = JSON.parse(fs.readFileSync(process.env.HABAB_QA_AUTH_FILE, 'utf8'));
    await run(() => page.evaluateOnNewDocument((a, base) => {
      localStorage.setItem('hassTokens', JSON.stringify({ ...a, hassUrl: base,
        clientId: a.client_id, expires: Date.now() + a.expires_in * 1000 }));
      localStorage.setItem('selectedLanguage', JSON.stringify('ru'));
    }, auth, new URL(target).origin));
  }
  report.stage = 'initial_page';
  await run(() => page.goto(target, { waitUntil: 'networkidle2', timeout: 45000 }));
  report.stage = 'authenticated';
  await run(() => page.waitForFunction(() => document.querySelector('home-assistant')?.hass?.user,
    { timeout: 30000 }));
  report.stage = 'core_running';
  await run(() => page.waitForFunction(() => document.querySelector('home-assistant')?.hass?.config?.state === 'RUNNING', { timeout: 120000 }));
  report.stage = 'home_panel';
  const home = await run(() => page.evaluate(() => {
    const h = document.querySelector('home-assistant').hass;
    const panel = Object.values(h.panels).find((p) => p.component_name === 'home');
    return panel ? `/${panel.url_path}/overview` : null;
  }));
  if (!home) throw new Error('home_panel_unavailable');
  await run(() => page.goto(new URL(home, target).href, { waitUntil: 'networkidle2', timeout: 45000 }));
  report.stage = 'native_cards';
  await run(() => page.waitForFunction(() => {
    const find = (root) => [...root.querySelectorAll('*')].some((e) =>
      e.localName === 'hui-tile-card' || (e.shadowRoot && find(e.shadowRoot)));
    return find(document);
  }, { timeout: 15000 }));
  report.render = await run(() => page.evaluate(() => {
    const visibleVacuumCards = [], commandFeatures = [];
    const scan = (root) => {
      for (const e of root.querySelectorAll('*')) {
        const rect = e.getBoundingClientRect();
        const cfg = e.config || e._config;
        if (e.localName === 'hui-tile-card' && cfg?.entity?.startsWith('vacuum.') && rect.width && rect.height) {
          visibleVacuumCards.push({ width: Math.round(rect.width), height: Math.round(rect.height),
            icon: cfg.icon, commands: cfg.features?.flatMap((f) => f.commands || []) || [], features: cfg.features?.map((f) => f.type) || [] });
        }
        if (e.localName === 'hui-vacuum-commands-card-feature' && rect.width && rect.height) {
          commandFeatures.push({ width: Math.round(rect.width), height: Math.round(rect.height) });
        }
        if (e.shadowRoot) scan(e.shadowRoot);
      }
    };
    scan(document);
    return { path: location.pathname, language: document.querySelector('home-assistant')?.hass?.locale?.language, visibleVacuumCards, commandFeatures };
  }));
  await run(() => page.screenshot({ path: output + '.png' }));
  report.verdict = report.render.visibleVacuumCards.length && report.render.commandFeatures.length
    ? 'NATIVE_VACUUM_VISIBLE_WITH_CONTROLS' : 'NATIVE_VACUUM_NOT_READY';
};
(async () => {
  let client;
  try {
    if (local) {
      await withOwnedChromium(puppeteer, { taskTimeoutMs: 210000,
        args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'] }, async ({ page }) => {
        try { await check(page, (fn) => fn()); }
        catch (e) { report.pageState = await page.evaluate(() => ({ path: location.pathname, authenticated: !!document.querySelector('home-assistant')?.hass?.user })); await page.screenshot({ path: output + '.png' }); throw e; }
        finally { await page.evaluate(() => localStorage.removeItem('hassTokens')).catch(() => {}); }
      });
    } else {
      client = await require('/root/kolya/chrome_tunnel_client.cjs').connect();
      const page = await client.newPage();
      await check(page, (fn) => client.run(page, fn));
    }
  } catch (e) { report.verdict = 'CHECK_FAILED'; report.error = e.code || e.name; }
  finally {
    if (client) await client.close();
    fs.writeFileSync(output + '.json', JSON.stringify(report, null, 2) + '\n');
  }
  process.stdout.write(JSON.stringify(report) + '\n');
})();
