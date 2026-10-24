const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const puppeteer = require('/root/homeassistent/habab/node_modules/puppeteer-core');

const RELEASE_READY = process.env.HABAB_PROVIDER_STATUS_RELEASE_READY;
const ALLOW_CHECK = process.env.HABAB_PROVIDER_STATUS_ALLOW_CHECK;
const RUN_SELECTED_CHECK = process.argv.includes('--check-selected');
const ORIGIN = 'https://habab-demo.wpmix.net';
const OWNER_ROUTE = `${ORIGIN}/owner-test`;
const AI_ROUTE = `${ORIGIN}/habab-ai`;
const CDP = 'http://127.0.0.1:9222';
const AUTH_FILE = '/root/.local/share/habab-owner-test/qa-current-auth.json';
const OUTPUT_ROOT = path.join(__dirname, 'evidence');
const RUN_ID = new Date().toISOString().replaceAll(':', '-');
const OUTPUT = path.join(OUTPUT_ROOT, RUN_ID);

if (RELEASE_READY !== '0.3.5') {
  throw new Error('Wait for the parent 0.3.5 release signal and set the release gate.');
}
if (RUN_SELECTED_CHECK && ALLOW_CHECK !== 'yes') {
  throw new Error('The selected-provider API check requires explicit parent QA authorization.');
}
if (!fs.existsSync(AUTH_FILE)) {
  throw new Error('Refresh private owner-test auth before running this QA.');
}
const authStat = fs.lstatSync(AUTH_FILE);
if (!authStat.isFile() || (authStat.mode & 0o077) !== 0) {
  throw new Error('Owner-test auth file must have mode 0600.');
}

const waitForPanel = async (page) => {
  await page.waitForFunction(() => {
    const visit = (root) => {
      const panel = root.querySelector('habab-ai-panel');
      if (panel) return panel;
      for (const element of root.querySelectorAll('*')) {
        if (element.shadowRoot) {
          const nested = visit(element.shadowRoot);
          if (nested) return nested;
        }
      }
      return null;
    };
    const panel = visit(document);
    if (panel) window.__providerStatusPanel = panel;
    return panel?.connectionsLoaded === true;
  }, { timeout: 45000 });
};

const providerNames = { habab: 'Habab', openai: 'OpenAI', deepseek: 'DeepSeek', gemini: 'Google Gemini' };

async function main() {
  fs.mkdirSync(OUTPUT, { recursive: true });
  const report = {
    target: AI_ROUTE,
    releaseGate: RELEASE_READY,
    viewport: [1280, 800],
    checks: {},
    liveStatuses: [],
    screenshots: [],
  };
  let browser;
  const contexts = [];

  try {
    const auth = JSON.parse(fs.readFileSync(AUTH_FILE, 'utf8'));
    browser = await puppeteer.connect({ browserURL: CDP });

    const context = await browser.createBrowserContext();
    contexts.push(context);
    const page = await context.newPage();
    await page.setViewport({ width: 1280, height: 800 });
    await page.evaluateOnNewDocument((tokens, origin) => {
      localStorage.setItem('hassTokens', JSON.stringify({
        ...tokens,
        hassUrl: origin,
        clientId: tokens.client_id,
        expires: Date.now() + tokens.expires_in * 1000,
      }));
      localStorage.setItem('selectedLanguage', JSON.stringify('ru'));
    }, auth, ORIGIN);

    // This first same-origin GET selects the native owner-test backend.
    await page.goto(OWNER_ROUTE, { waitUntil: 'networkidle2', timeout: 45000 });
    await page.goto(AI_ROUTE, { waitUntil: 'networkidle2', timeout: 45000 });
    await waitForPanel(page);

    const initial = await page.evaluate(() => {
      const panel = window.__providerStatusPanel;
      return {
        isAdmin: panel._hass.user?.is_admin === true,
        entries: panel.connections.map(({ entry_id, provider, model, assistant_selected, api_check_status, api_check_at }) => ({
          entry_id, provider, model, assistant_selected, api_check_status, api_check_at,
        })),
        assistantSummary: panel.q('#assistant-provider-status').textContent,
      };
    });
    assert.equal(initial.isAdmin, true, 'owner-test account must be an administrator');
    assert(initial.entries.length > 0, 'native provider list must contain a saved entry');
    assert(initial.entries.every((entry) => ['verified', 'rejected', 'unknown'].includes(entry.api_check_status)),
      'the deployed integration must return sanitized API-check status');
    const selected = initial.entries.filter((entry) => entry.assistant_selected);
    assert.equal(selected.length, 1, 'exactly one persisted provider must be selected for assistant chat');
    const selectedEntry = selected[0];
    const selectedSummary = `Для чата выбрано: ${providerNames[selectedEntry.provider] || selectedEntry.provider}, модель ${selectedEntry.model}.`;
    assert.equal(initial.assistantSummary, selectedSummary);
    report.checks.persistedProviderModel = {
      provider: selectedEntry.provider,
      model: selectedEntry.model,
      summaryMatched: true,
    };

    await page.evaluate(async (provider) => {
      const panel = window.__providerStatusPanel;
      panel.provider = provider;
      panel.selectProvider();
    }, selectedEntry.provider);
    await page.waitForFunction((provider) => {
      const panel = window.__providerStatusPanel;
      return panel.provider === provider && panel.q('#key').value.length > 0;
    }, { timeout: 20000 }, selectedEntry.provider);
    const keyReadback = await page.evaluate(async () => {
      const panel = window.__providerStatusPanel;
      const entry = panel.entry();
      const saved = await panel._hass.callWS({ type: 'habab_home/providers/key', entry_id: entry.entry_id });
      const input = panel.q('#key');
      return {
        adminPlainText: panel._hass.user?.is_admin === true && input.type === 'text',
        matchesSavedReadback: input.value === saved.api_key,
      };
    });
    assert.equal(keyReadback.adminPlainText, true);
    assert.equal(keyReadback.matchesSavedReadback, true);
    report.checks.adminSavedKeyReadback = keyReadback;

    const unknownEntry = initial.entries.find((entry) => entry.api_check_status === 'unknown');
    if (unknownEntry) {
      const staleOverride = await page.evaluate((entryId, provider) => {
        const panel = window.__providerStatusPanel;
        panel.provider = provider;
        panel.selectProvider();
        const entry = panel.connections.find((item) => item.entry_id === entryId);
        panel.connectionCheckStates.set(panel.checkStateKey(entry), 'verified');
        panel.updateControls();
        const stayedUnknown = panel.q('#api-status').textContent.includes('Статус API неизвестен');
        panel.connectionCheckStates.delete(panel.checkStateKey(entry));
        panel.updateControls();
        return stayedUnknown;
      }, unknownEntry.entry_id, unknownEntry.provider);
      assert.equal(staleOverride, true, 'a stale local terminal value must not override server unknown');
      report.checks.staleTerminalOverrideIgnored = true;
    } else {
      report.checks.staleTerminalOverrideIgnored = 'not observed: no native entry has unknown status';
    }

    if (RUN_SELECTED_CHECK) {
      await page.evaluate((provider) => {
        const panel = window.__providerStatusPanel;
        panel.provider = provider;
        panel.selectProvider();
        panel.q('#check').click();
      }, selectedEntry.provider);
      await page.waitForFunction((entryId) => {
        const panel = window.__providerStatusPanel;
        const entry = panel.connections.find((item) => item.entry_id === entryId);
        return !panel.busy && entry?.api_check_status === 'verified' && !!entry.api_check_at;
      }, { timeout: 60000 }, selectedEntry.entry_id);
      const beforeReload = await page.evaluate((entryId) => {
        const entry = window.__providerStatusPanel.connections.find((item) => item.entry_id === entryId);
        return { provider: entry.provider, model: entry.model, status: entry.api_check_status, checkedAt: entry.api_check_at };
      }, selectedEntry.entry_id);
      assert.equal(beforeReload.status, 'verified');
      assert(beforeReload.checkedAt);

      await page.reload({ waitUntil: 'networkidle2', timeout: 45000 });
      await waitForPanel(page);
      const afterReload = await page.evaluate((entryId) => {
        const panel = window.__providerStatusPanel;
        const entry = panel.connections.find((item) => item.entry_id === entryId);
        return {
          entry: entry && { provider: entry.provider, model: entry.model, status: entry.api_check_status, checkedAt: entry.api_check_at },
          summary: panel.q('#assistant-provider-status').textContent,
        };
      }, selectedEntry.entry_id);
      assert.deepEqual(afterReload.entry, beforeReload, 'verified status and timestamp must persist through reload');
      assert.equal(afterReload.summary, selectedSummary);
      report.checks.verifiedTimestampSurvivesReload = true;
      report.checks.verifiedTimestamp = beforeReload.checkedAt;
    } else {
      report.checks.verifiedTimestampSurvivesReload = 'not run: --check-selected was not authorized';
    }

    const liveStatuses = await page.evaluate(() => {
      const panel = window.__providerStatusPanel;
      const observations = [];
      for (const entry of panel.connections) {
        panel.provider = entry.provider;
        panel.selectProvider();
        const label = panel.q('#api-status').textContent;
        observations.push({ provider: entry.provider, status: entry.api_check_status, checkedAt: entry.api_check_at, labelMatches: (
          (entry.api_check_status === 'verified' && label.includes('прошли проверку API')) ||
          (entry.api_check_status === 'rejected' && label.includes('отклонил сохранённый ключ')) ||
          (entry.api_check_status === 'unknown' && (label.includes('Статус API неизвестен') || label.includes('не удалось подтвердить')))
        ) });
      }
      return observations;
    });
    assert(liveStatuses.every((item) => item.labelMatches), 'UI labels must reflect each server readback status');
    report.liveStatuses = liveStatuses;
    report.checks.rejectedStateObserved = liveStatuses.some((item) => item.status === 'rejected')
      ? true : 'not observed; live credentials were not altered';

    const tabIndependence = await page.evaluate((selectedProvider, expectedSummary) => {
      const panel = window.__providerStatusPanel;
      const otherProvider = ['habab', 'openai', 'deepseek', 'gemini'].find((item) => item !== selectedProvider);
      panel.shadowRoot.querySelector(`[data-provider="${otherProvider}"]`).click();
      return {
        editorProvider: panel.provider,
        selectedSummary: panel.q('#assistant-provider-status').textContent,
        unchanged: panel.q('#assistant-provider-status').textContent === expectedSummary,
      };
    }, selectedEntry.provider, selectedSummary);
    if (typeof tabIndependence === 'object') assert.equal(tabIndependence.unchanged, true);
    report.checks.selectedProviderIndependentOfEditorTab = tabIndependence;

    const pipelineIsolation = await page.evaluate(async (expectedSummary) => {
      const panel = window.__providerStatusPanel;
      const realHass = panel._hass;
      const shim = Object.create(realHass);
      shim.callWS = (message) => message.type === 'assist_pipeline/pipeline/list'
        ? Promise.reject({ code: 'qa_pipeline_unavailable' })
        : realHass.callWS(message);
      panel._hass = shim;
      try {
        await panel.refresh();
        return {
          connectionsLoaded: panel.connectionsLoaded,
          providerSummary: panel.q('#assistant-provider-status').textContent,
          connectionStatus: panel.q('#connection-status').textContent,
          summaryUnchanged: panel.q('#assistant-provider-status').textContent === expectedSummary,
          connectionStatusAvailable: panel.q('#connection-status').textContent !== panel.t('connectionStatusUnavailable'),
        };
      } finally {
        panel._hass = realHass;
        await panel.refresh();
      }
    }, selectedSummary);
    assert.equal(pipelineIsolation.connectionsLoaded, true);
    assert.equal(pipelineIsolation.summaryUnchanged, true);
    assert.equal(pipelineIsolation.connectionStatusAvailable, true);
    report.checks.pipelineFailureIsolation = pipelineIsolation;

    const anonymous = await browser.createBrowserContext();
    contexts.push(anonymous);
    const anonymousPage = await anonymous.newPage();
    await anonymousPage.setViewport({ width: 1280, height: 800 });
    await anonymousPage.goto(OWNER_ROUTE, { waitUntil: 'networkidle2', timeout: 45000 });
    await anonymousPage.goto(AI_ROUTE, { waitUntil: 'networkidle2', timeout: 45000 });
    const anonymousCanSeeKeyInput = await anonymousPage.evaluate(() => {
      const visit = (root) => {
        const panel = root.querySelector('habab-ai-panel');
        if (panel) return !!panel.shadowRoot?.querySelector('#key');
        for (const element of root.querySelectorAll('*')) {
          if (element.shadowRoot) {
            const nested = visit(element.shadowRoot);
            if (nested) return nested;
          }
        }
        return false;
      };
      return visit(document);
    });
    assert.equal(anonymousCanSeeKeyInput, false, 'an unauthenticated context must not see the admin key input');
    report.checks.anonymousKeyFieldAbsent = true;

    const masked = await page.evaluate(() => {
      const panel = window.__providerStatusPanel;
      const key = panel.q('#key');
      key.type = 'password';
      key.value = '';
      return key.type === 'password' && key.value === '';
    });
    assert.equal(masked, true);
    const screenshot = path.join(OUTPUT, 'owner-provider-status-masked.png');
    await page.screenshot({ path: screenshot, fullPage: true });
    report.screenshots.push(path.basename(screenshot));
    report.passed = true;

    await page.evaluate(() => localStorage.removeItem('hassTokens'));
    await anonymousPage.evaluate(() => localStorage.removeItem('hassTokens'));
    fs.writeFileSync(path.join(OUTPUT, 'report.json'), JSON.stringify(report, null, 2), { mode: 0o600 });
    process.stdout.write(JSON.stringify({ passed: true, evidence: path.relative(__dirname, OUTPUT), checks: report.checks }) + '\n');
  } catch (error) {
    report.passed = false;
    report.error = { name: error.name, code: error.code || null };
    fs.writeFileSync(path.join(OUTPUT, 'report.json'), JSON.stringify(report, null, 2), { mode: 0o600 });
    process.stderr.write(JSON.stringify({ passed: false, evidence: path.relative(__dirname, OUTPUT), error: report.error }) + '\n');
    process.exitCode = 1;
  } finally {
    for (const context of contexts) {
      await context.close().catch(() => {});
    }
    if (browser) browser.disconnect();
  }
}

main().catch((error) => {
  process.stderr.write(JSON.stringify({ passed: false, error: { name: error.name, code: error.code || null } }) + '\n');
  process.exitCode = 1;
});
