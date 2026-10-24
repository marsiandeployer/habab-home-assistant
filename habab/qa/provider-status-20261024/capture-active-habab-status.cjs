const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const puppeteer = require('/root/homeassistent/habab/node_modules/puppeteer-core');

const ORIGIN = 'https://habab-demo.wpmix.net';
const OWNER_ROUTE = `${ORIGIN}/owner-test`;
const AI_ROUTE = `${ORIGIN}/habab-ai`;
const AUTH_FILE = '/root/.local/share/habab-owner-test/qa-current-auth.json';
const CDP = 'http://127.0.0.1:9222';
const OUT = path.join(__dirname, 'evidence');
const RUN = new Date().toISOString().replaceAll(':', '-');
const RUN_DIR = path.join(OUT, `active-habab-${RUN}`);

if (!fs.existsSync(AUTH_FILE)) throw new Error('Refresh private owner-test auth first.');
const authStat = fs.lstatSync(AUTH_FILE);
if (!authStat.isFile() || (authStat.mode & 0o077) !== 0) throw new Error('Owner auth must be a regular 0600 file.');

const waitForPanel = async (page) => page.waitForFunction(() => {
  const find = (root) => {
    const panel = root.querySelector('habab-ai-panel');
    if (panel) return panel;
    for (const element of root.querySelectorAll('*')) {
      if (element.shadowRoot) {
        const nested = find(element.shadowRoot);
        if (nested) return nested;
      }
    }
    return null;
  };
  const panel = find(document);
  if (panel) window.__activeProviderPanel = panel;
  return panel?.connectionsLoaded === true;
}, { timeout: 45000 });

async function main() {
  fs.mkdirSync(RUN_DIR, { recursive: true });
  const tokens = JSON.parse(fs.readFileSync(AUTH_FILE, 'utf8'));
  const report = {
    target: AI_ROUTE,
    viewport: [1280, 800],
    selectedProvider: null,
    model: null,
    apiStatus: null,
    apiCheckAt: null,
    editorTab: 'habab',
    chatClosed: false,
    keyField: { type: 'password', emptyBeforeCapture: false },
    forbiddenCommands: { providerCheck: 0, providerSave: 0, chatRun: 0, chatAbort: 0 },
    screenshot: 'active-habab-status-masked.png',
  };
  let browser;
  let context;
  let page;

  try {
    browser = await puppeteer.connect({ browserURL: CDP });
    context = await browser.createBrowserContext();
    page = await context.newPage();
    await page.setViewport({ width: 1280, height: 800 });
    await page.evaluateOnNewDocument((value, origin) => {
      localStorage.setItem('hassTokens', JSON.stringify({
        ...value,
        hassUrl: origin,
        clientId: value.client_id,
        expires: Date.now() + value.expires_in * 1000,
      }));
      localStorage.setItem('selectedLanguage', JSON.stringify('ru'));
    }, tokens, ORIGIN);

    await page.goto(OWNER_ROUTE, { waitUntil: 'networkidle2', timeout: 45000 });
    await page.goto(AI_ROUTE, { waitUntil: 'networkidle2', timeout: 45000 });
    await waitForPanel(page);

    const active = await page.evaluate(() => {
      const panel = window.__activeProviderPanel;
      return {
        admin: panel._hass.user?.is_admin === true,
        selected: panel.connections.filter((item) => item.assistant_selected)
          .map(({ entry_id, provider, model, api_check_status, api_check_at }) => ({
            entry_id, provider, model, api_check_status, api_check_at,
          })),
      };
    });
    assert.equal(active.admin, true, 'owner-test context must be an administrator');
    assert.equal(active.selected.length, 1, 'expected one persisted assistant provider');
    const entry = active.selected[0];
    assert.equal(entry.provider, 'habab');
    assert.equal(entry.model, 'gemini-3.1-flash-lite');
    assert.equal(entry.api_check_status, 'verified');
    assert(entry.api_check_at);

    const forbidden = await page.evaluate(() => {
      const panel = window.__activeProviderPanel;
      const hass = panel._hass;
      const rawCallWS = hass.callWS;
      const counts = { providerCheck: 0, providerSave: 0, chatRun: 0, chatAbort: 0 };
      hass.callWS = function (message, ...args) {
        if (message.type === 'habab_home/providers/check') counts.providerCheck++;
        if (message.type === 'habab_home/providers/save') counts.providerSave++;
        if (message.type === 'habab_home/agent/run') counts.chatRun++;
        if (message.type === 'habab_home/agent/abort') counts.chatAbort++;
        return rawCallWS.call(hass, message, ...args);
      };
      window.__forbiddenCounts = counts;
      return true;
    });
    assert.equal(forbidden, true);

    const chatClosed = await page.evaluate(() => {
      const chat = document.querySelector('habab-setup-chat');
      if (!chat || typeof chat.q !== 'function') return false;
      chat.q('.close').click();
      return chat.q('dialog')?.open === false;
    });
    assert.equal(chatClosed, true, 'close the chat overlay without resetting the conversation');
    report.chatClosed = true;

    await page.evaluate(() => {
      const panel = window.__activeProviderPanel;
      panel.shadowRoot.querySelector('[data-provider="habab"]').click();
    });
    await page.waitForFunction(() => {
      const panel = window.__activeProviderPanel;
      return panel.provider === 'habab' && panel.q('#key').value.length > 0;
    }, { timeout: 20000 });

    const visibleStatus = await page.evaluate((entryId) => {
      const panel = window.__activeProviderPanel;
      const selected = panel.connections.find((item) => item.entry_id === entryId);
      const summary = panel.q('#assistant-provider-status').textContent;
      const apiStatus = panel.q('#api-status').textContent;
      const formatted = new Intl.DateTimeFormat(panel.language, {
        dateStyle: 'medium', timeStyle: 'short',
      }).format(new Date(selected.api_check_at));
      return {
        selectedProvider: selected.provider,
        model: selected.model,
        summary,
        apiStatus,
        apiCheckAt: selected.api_check_at,
        dateVisible: apiStatus.includes(formatted),
        keyLoadedInMemory: panel.q('#key').value.length > 0,
        editorTab: panel.provider,
      };
    }, entry.entry_id);
    assert(visibleStatus.summary.includes('Habab'));
    assert(visibleStatus.summary.includes('gemini-3.1-flash-lite'));
    assert(visibleStatus.apiStatus.includes('прошли проверку API'));
    assert.equal(visibleStatus.dateVisible, true);
    assert.equal(visibleStatus.keyLoadedInMemory, true);
    assert.equal(visibleStatus.editorTab, 'habab');
    report.selectedProvider = visibleStatus.selectedProvider;
    report.model = visibleStatus.model;
    report.apiStatus = 'verified';
    report.apiCheckAt = visibleStatus.apiCheckAt;
    report.summaryVisible = true;
    report.apiCheckDateVisible = true;
    report.keyLoadedForAdmin = true;

    const masked = await page.evaluate(() => {
      const panel = window.__activeProviderPanel;
      panel.keyFetchGeneration += 1;
      panel.keyVisibilityPreference = 'hidden';
      const key = panel.q('#key');
      key.value = '';
      key.type = 'password';
      panel.scrollTop = 0;
      window.scrollTo(0, 0);
      return { password: key.type === 'password', empty: key.value === '' };
    });
    assert.equal(masked.password, true);
    assert.equal(masked.empty, true);
    report.keyField.emptyBeforeCapture = true;

    const screenshot = path.join(RUN_DIR, report.screenshot);
    await page.screenshot({ path: screenshot, fullPage: false });
    const counts = await page.evaluate(() => window.__forbiddenCounts);
    assert.deepEqual(counts, { providerCheck: 0, providerSave: 0, chatRun: 0, chatAbort: 0 });
    report.forbiddenCommands = counts;
    report.passed = true;
    fs.writeFileSync(path.join(RUN_DIR, 'receipt.json'), JSON.stringify(report, null, 2), { mode: 0o600 });
    process.stdout.write(JSON.stringify({ passed: true, evidence: path.relative(__dirname, RUN_DIR), checks: report }) + '\n');
  } catch (error) {
    report.passed = false;
    report.error = { name: error.name, code: error.code || null };
    fs.writeFileSync(path.join(RUN_DIR, 'receipt.json'), JSON.stringify(report, null, 2), { mode: 0o600 });
    process.stderr.write(JSON.stringify({ passed: false, evidence: path.relative(__dirname, RUN_DIR), error: report.error }) + '\n');
    process.exitCode = 1;
  } finally {
    if (page) await page.close().catch(() => {});
    if (context) await context.close().catch(() => {});
    if (browser) browser.disconnect();
  }
}

main().catch((error) => {
  process.stderr.write(JSON.stringify({ passed: false, error: { name: error.name, code: error.code || null } }) + '\n');
  process.exitCode = 1;
});
