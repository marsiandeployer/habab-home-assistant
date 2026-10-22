const fs = require('fs');
const path = require('path');
const os = require('os');
const assert = require('assert/strict');
const puppeteer = require('puppeteer-core');

// Only the isolated localhost instance created for this task is accepted.
const runtime = process.env.HABAB_QA_RUNTIME;
if (!runtime) throw new Error('Set HABAB_QA_RUNTIME to a private QA runtime directory');
const {port} = JSON.parse(fs.readFileSync(path.join(runtime, 'qa-port.json')));
const auth = JSON.parse(fs.readFileSync(path.join(runtime, 'qa-auth.json')));
const base = `http://127.0.0.1:${Number(port)}`;
const output = path.join(__dirname, 'voice');
fs.mkdirSync(output, {recursive: true});

(async () => {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-habab-voice-'));
  const snapProfile = path.join('/tmp/snap-private-tmp/snap.chromium/tmp', path.basename(profile));
  fs.mkdirSync(snapProfile, {recursive: true});
  const fixture = path.join(profile, 'microphone.wav');
  for (const dir of [profile, snapProfile]) {
    fs.copyFileSync(path.join(output, 'microphone-fixture.wav'), path.join(dir, 'microphone.wav'));
  }
  let browser;
  const report = {cases: []};
  try {
    browser = await puppeteer.launch({executablePath: '/snap/bin/chromium', headless: true,
      userDataDir: profile, args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
        '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
        `--use-file-for-fake-audio-capture=${fixture}`]});
    for (const [mode, width, height] of [['desktop', 1280, 800], ['mobile', 390, 844]]) {
      const context = await browser.createBrowserContext();
      try {
        const page = await context.newPage();
        const errors = [];
        page.on('pageerror', (error) => errors.push(error.stack || error.message));
        const protocol = await page.createCDPSession();
        await protocol.send('Network.enable');
        protocol.on('Network.webSocketFrameReceived', ({response}) => {
          try {
            const data = JSON.parse(response.payloadData);
            for (const item of Array.isArray(data) ? data : [data]) {
              if (item.type === 'result' && item.success === false) {
                (report.protocolErrors ||= []).push({id: item.id, code: item.error.code});
              }
            }
          } catch (_) { /* Binary audio is not JSON. */ }
        });
        await page.setViewport({width, height, isMobile: mode === 'mobile', hasTouch: mode === 'mobile'});
        await page.evaluateOnNewDocument((tokens, url) => {
          window.qaAsyncErrors = [];
          window.addEventListener('unhandledrejection', (event) => window.qaAsyncErrors.push({code: event.reason?.code, stage: window.qaStage}));
          localStorage.setItem('hassTokens', JSON.stringify({...tokens, hassUrl: url,
            clientId: `${url}/`, expires: Date.now() + tokens.expires_in * 1000}));
          localStorage.setItem('selectedLanguage', JSON.stringify('ru'));
        }, auth, base);
        await page.goto(base + '/habab-ai', {waitUntil: 'networkidle2', timeout: 45000});
        await page.waitForFunction(() => {
          const panel = document.querySelector('home-assistant')?.shadowRoot
            ?.querySelector('home-assistant-main')?.shadowRoot?.querySelector('ha-panel-custom');
          const walk = (root) => {
            const found = root.querySelector('habab-ai-panel');
            if (found) return found;
            for (const item of root.querySelectorAll('*')) {
              if (item.shadowRoot) {const nested = walk(item.shadowRoot); if (nested) return nested;}
            }
          };
          const el = walk(document);
          return el?.connections?.length >= 2;
        }, {timeout: 45000});
        await page.evaluate(() => {
          const walk = (root) => {
            const found = root.querySelector('habab-ai-panel');
            if (found) return found;
            for (const item of root.querySelectorAll('*')) {
              if (item.shadowRoot) {const nested = walk(item.shadowRoot); if (nested) return nested;}
            }
          };
          window.qaPanel = walk(document);
        });
        const el = async (selector) => (await page.evaluateHandle((selector) => window.qaPanel.shadowRoot.querySelector(selector), selector)).asElement();
        for (const provider of ['openai', 'deepseek', 'gemini']) {
          await (await el(`[data-provider="${provider}"]`)).click();
          const fields = await page.evaluate(() => {
            const p = window.qaPanel;
            return {title: p.q('#provider-title').textContent, steps: p.q('#guide').children.length,
              keyEmpty: p.q('#key').value === '', password: p.q('#key').type === 'password',
              url: p.q('#key-link').href};
          });
          assert.equal(fields.steps, 3);
          assert(fields.keyEmpty && fields.password && fields.url.startsWith('https://'));
        }
        await (await el('[data-provider="deepseek"]')).click();
        await page.screenshot({path: path.join(output, `${mode}-providers.png`), fullPage: true});
        if (mode === 'desktop') {
          // Invalid credentials must not replace the already working saved key.
          await (await el('#key')).type('invalid-key-for-qa');
          await page.evaluate(() => { window.qaStage = 'invalid-key'; });
          await (await el('#reveal')).click();
          assert.equal(await page.evaluate(() => window.qaPanel.q('#key').type), 'text');
          await (await el('#reveal')).click();
          await (await el('button[type="submit"]')).click();
          await page.waitForFunction(() => !window.qaPanel.busy);
          assert.equal(await page.evaluate(() => window.qaPanel.q('#feedback').dataset.error), 'true');
          await (await el('[data-provider="openai"]')).click();
          assert.equal(await page.evaluate(() => window.qaPanel.q('#key').value), '');
          await (await el('[data-provider="deepseek"]')).click();
          await (await el('#check')).click();
          await page.evaluate(() => { window.qaStage = 'check-saved'; });
          await page.waitForFunction(() => !window.qaPanel.busy);
          assert.equal(await page.evaluate(() => window.qaPanel.q('#feedback').dataset.error), 'false');
          await (await el('#command')).type('Выключи тестовую лампу');
          await (await el('#send')).click();
          await page.evaluate(() => { window.qaStage = 'text-command'; });
          await page.waitForFunction(() => !window.qaPanel.busy);
          assert.equal(await page.evaluate(() => window.qaPanel._hass.states['light.testovaia_lampa'].state), 'off');
          await (await el('#mic')).click();
          await page.evaluate(() => { window.qaStage = 'microphone'; });
          await page.waitForFunction(() => !window.qaPanel.busy, {timeout: 90000});
          const voice = await page.evaluate(() => ({
            transcript: window.qaPanel.q('#command').value,
            state: window.qaPanel._hass.states['light.testovaia_lampa'].state,
            response: window.qaPanel.q('#answer').textContent,
            error: window.qaPanel.q('#answer').dataset.error,
            microphoneStopped: window.qaPanel.voice?.stream?.getTracks().every((track) => track.readyState === 'ended'),
          }));
          assert.equal(voice.state, 'on');
          assert.equal(voice.error, 'false');
          assert(voice.microphoneStopped);
          report.browserVoice = voice;
          await page.screenshot({path: path.join(output, 'desktop-voice-result.png'), fullPage: true});
        }
        const overflow = await page.evaluate(() => {
          const panel = window.qaPanel;
          return panel.scrollWidth > panel.clientWidth + 1 || panel.shadowRoot.querySelector('main').scrollWidth > panel.clientWidth + 1;
        });
        assert(!overflow);
        report.asyncErrors = await page.evaluate(() => window.qaAsyncErrors);
        assert.deepEqual(errors, []);
        report.cases.push({mode, providers: 3, guides: true, passwordField: true, noHorizontalOverflow: true, errors});
      } finally { await context.close(); }
    }
    report.passed = true;
  } catch (error) {
    report.passed = false;
    report.error = error.stack;
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
    fs.rmSync(profile, {recursive: true, force: true});
    if (fs.existsSync(snapProfile) && !fs.lstatSync(snapProfile).isSymbolicLink()) fs.rmSync(snapProfile, {recursive: true, force: true});
    fs.writeFileSync(path.join(output, 'browser.json'), JSON.stringify(report, null, 2) + '\n');
  }
  console.log(JSON.stringify(report));
})();
