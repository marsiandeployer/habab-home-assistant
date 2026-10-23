const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const puppeteer = require('../../node_modules/puppeteer-core');
const url = 'https://habab-demo.wpmix.net/?habab-preview=onboarding';

(async () => {
  const browser = await puppeteer.connect({browserURL: 'http://127.0.0.1:9222'});
  const initial = (await browser.pages()).map(page => page.target()._targetId);
  const context = await browser.createBrowserContext();
  const report = {url, errors: [], native_auth_requests: [], native_websockets: [], bundles: []};
  try {
    const page = await context.newPage();
    page.on('pageerror', error => report.errors.push(error.message));
    page.on('request', request => {
      if (new URL(request.url()).pathname === '/auth/token') report.native_auth_requests.push(request.url());
      if (request.resourceType() === 'script' && /\/(?:core|main)\.[a-f0-9]+\.js/.test(request.url())) report.bundles.push(request.url());
    });
    const cdp = await page.createCDPSession();
    await cdp.send('Network.enable');
    cdp.on('Network.webSocketCreated', event => report.native_websockets.push(event.url));
    for (const viewport of [{width:1280,height:800}, {width:390,height:844,isMobile:true,hasTouch:true}]) {
      await page.setViewport(viewport);
      await page.goto(url, {waitUntil:'networkidle2', timeout:45000});
      await page.waitForFunction(() => {
        const host = document.querySelector('habab-setup-chat');
        return host?.sessionId && host.previewState?.status === 'ready';
      });
      const state = await page.evaluate(async () => {
        const root = document.querySelector('habab-setup-chat').shadowRoot;
        const input = root.querySelector('textarea').getBoundingClientRect();
        const send = root.querySelector('.send').getBoundingClientRect();
        return {open:root.querySelector('dialog').open, rows:root.querySelectorAll('.preview-row').length, input_bottom:input.bottom, send_bottom:send.bottom, viewport_height:visualViewport.height, registrations:(await navigator.serviceWorker.getRegistrations()).length};
      });
      assert.equal(state.open, true);
      assert.equal(state.rows, 3);
      assert.equal(state.registrations, 0);
      assert.ok(state.input_bottom <= state.viewport_height);
      assert.ok(state.send_bottom <= state.viewport_height);
      report[viewport.width === 1280 ? 'desktop' : 'mobile'] = state;
      await page.screenshot({path:path.join(__dirname, viewport.width === 1280 ? 'live-desktop.png' : 'live-mobile.png')});
    }
    assert.equal(report.errors.length, 0);
    assert.equal(report.native_auth_requests.length, 0);
    assert.equal(report.native_websockets.length, 0);
    assert.ok(report.bundles.some(bundle => bundle.includes('/releases/20261021-ui5/')));
    report.verdict = 'PASS';
  } finally {
    await context.close();
    report.context_closed = true;
    report.existing_pages_preserved = (await browser.pages()).filter(page => initial.includes(page.target()._targetId)).length === initial.length;
    await browser.disconnect();
    fs.writeFileSync(path.join(__dirname,'live-result.json'), JSON.stringify(report,null,2)+'\n');
  }
  process.stdout.write(JSON.stringify(report)+'\n');
})().catch(error => {process.stderr.write(error.stack+'\n');process.exitCode=1;});
