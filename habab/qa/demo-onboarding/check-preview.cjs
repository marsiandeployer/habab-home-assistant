const fs = require('node:fs');
const path = require('node:path');
const puppeteer = require('../../node_modules/puppeteer-core');

const out = __dirname;
const url = process.env.HABAB_QA_URL || 'https://habab-demo.wpmix.net/';
const stamp = process.env.HABAB_QA_STAMP || 'baseline';

(async () => {
  const browser = await puppeteer.connect({browserURL: 'http://127.0.0.1:9222'});
  const existing = (await browser.pages()).map(page => page.target()._targetId);
  const context = await browser.createBrowserContext();
  const report = {url, stamp, errors: [], existing_pages: existing.length};
  try {
    const page = await context.newPage();
    page.on('pageerror', error => report.errors.push(error.message));
    await page.setViewport({width: 1280, height: 800});
    await page.goto(url, {waitUntil: 'networkidle2', timeout: 45000});
    await page.waitForFunction(() => !!document.querySelector('habab-setup-chat')?.shadowRoot?.querySelector('textarea'), {timeout: 30000});
    await page.screenshot({path: path.join(out, `${stamp}-desktop.png`)});
    report.desktop = await page.evaluate(() => {
      const host = document.querySelector('habab-setup-chat');
      const root = host.shadowRoot;
      const bounds = selector => {
        const node = root.querySelector(selector);
        if (!node) return null;
        const rect = node.getBoundingClientRect();
        return {x: rect.x, y: rect.y, width: rect.width, height: rect.height, hidden: node.hidden, disabled: node.disabled};
      };
      return {dialog_open: root.querySelector('dialog').open, composer: bounds('textarea'), send: bounds('.send'), text: (root.querySelector('#messages') || root.querySelector('#log'))?.innerText, private_hook: !!window.hababAssistant, focused: root.activeElement?.tagName, launcher_display: getComputedStyle(root.querySelector('.launcher')).display};
    });
    await page.setViewport({width: 390, height: 844, isMobile: true, hasTouch: true});
    await page.reload({waitUntil: 'networkidle2'});
    await page.waitForFunction(() => !!document.querySelector('habab-setup-chat')?.shadowRoot?.querySelector('textarea'), {timeout: 30000});
    await page.screenshot({path: path.join(out, `${stamp}-mobile.png`)});
    report.mobile = await page.evaluate(() => {
      const root = document.querySelector('habab-setup-chat').shadowRoot;
      const rect = root.querySelector('textarea').getBoundingClientRect();
      return {dialog_open: root.querySelector('dialog').open, composer_bottom: rect.bottom, viewport_height: innerHeight, page_width: document.documentElement.scrollWidth, viewport_width: innerWidth, focused: root.activeElement?.tagName, launcher_display: getComputedStyle(root.querySelector('.launcher')).display};
    });
  } finally {
    await context.close();
    report.cleanup_context_closed = true;
    report.existing_pages_preserved = (await browser.pages()).filter(page => existing.includes(page.target()._targetId)).length === existing.length;
    await browser.disconnect();
    fs.writeFileSync(path.join(out, `${stamp}-result.json`), JSON.stringify(report, null, 2) + '\n');
  }
  process.stdout.write(JSON.stringify(report) + '\n');
})().catch(error => {process.stderr.write(error.stack + '\n'); process.exitCode = 1;});
