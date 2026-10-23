const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const puppeteer = require('../../node_modules/puppeteer-core');
const url = 'https://habab-demo.wpmix.net/?habab-preview=onboarding';
const stamp = process.env.HABAB_QA_STAMP || 'final';
const report = {url, cases: [], errors: [], limitations: ['Viewport resize emulates keyboard space; no physical mobile keyboard was tested.', 'Only public virtual demo state and chat were used; no private HA or hardware commands.']};

async function ready(page) {
  await page.goto(url, {waitUntil: 'networkidle2', timeout: 45000});
  await page.waitForFunction(() => {
    const host = document.querySelector('habab-setup-chat');
    return host?.sessionId && !host.busy && host.previewState?.status === 'ready';
  }, {timeout: 30000});
}

async function turn(page, {quick, text, name}) {
  const response = page.waitForResponse(response => response.url().endsWith('/api/ha-demo/chat') && response.request().method() === 'POST', {timeout: 45000});
  if (quick !== undefined) {
    const handle = await page.evaluateHandle(index => document.querySelector('habab-setup-chat').shadowRoot.querySelectorAll('.quick-reply')[index], quick);
    await handle.asElement().click();
    await handle.dispose();
  } else {
    const input = await page.evaluateHandle(() => document.querySelector('habab-setup-chat').shadowRoot.querySelector('textarea'));
    await input.asElement().type(text);
    await input.asElement().press('Enter');
    await input.dispose();
  }
  const busy = await page.evaluate(() => {
    const root = document.querySelector('habab-setup-chat').shadowRoot;
    const send = root.querySelector('.send');
    return {send_visible: !send.hidden && send.getBoundingClientRect().height > 0, send_disabled: send.disabled};
  });
  const result = await response;
  const body = await result.json();
  assert.equal(result.status(), 200, `${name}: ${JSON.stringify(body)}`);
  await page.waitForFunction(() => !document.querySelector('habab-setup-chat').busy);
  const last = body.messages.at(-1);
  assert.equal(last.role, 'assistant');
  assert.ok(last.content.trim());
  assert.equal(busy.send_visible, true);
  report.cases.push({name, status: result.status(), sent: body.messages.at(-2)?.content, answer: last.content, busy});
}

(async () => {
  const browser = await puppeteer.connect({browserURL: 'http://127.0.0.1:9222'});
  const initial = (await browser.pages()).map(page => page.target()._targetId);
  const contexts = [];
  const newPage = async viewport => {
    const context = await browser.createBrowserContext();
    contexts.push(context);
    const page = await context.newPage();
    page.on('pageerror', error => report.errors.push(error.message));
    await page.setViewport(viewport || {width: 1280, height: 800});
    return page;
  };
  try {
    const page = await newPage();
    await ready(page);
    report.startup = await page.evaluate(() => {
      const host = document.querySelector('habab-setup-chat'), root = host.shadowRoot;
      return {open: root.querySelector('dialog').open, focused: root.activeElement?.tagName, launcher_hidden: getComputedStyle(root.querySelector('.launcher')).display === 'none', rows: root.querySelectorAll('.preview-row').length, preview_first: root.querySelector('#messages').firstElementChild.className, hook_absent: !window.hababAssistant, scrollY};
    });
    assert.equal(report.startup.focused === 'TEXTAREA', false);
    assert.equal(report.startup.launcher_hidden, true);
    assert.equal(report.startup.rows, 3);
    await page.screenshot({path: path.join(__dirname, 'final-desktop.png')});
    await page.evaluate(() => {document.querySelector('habab-setup-chat').shadowRoot.querySelector('textarea').value = 'Сохранить мой черновик';});
    const draftButton = await page.evaluateHandle(() => document.querySelector('habab-setup-chat').shadowRoot.querySelector('.quick-reply'));
    await draftButton.asElement().click();
    await draftButton.dispose();
    report.draft = await page.evaluate(() => ({value: document.querySelector('habab-setup-chat').shadowRoot.querySelector('textarea').value, messages: document.querySelector('habab-setup-chat').messages.length}));
    assert.ok(report.draft.value.startsWith('Сохранить мой черновик\n'));
    assert.equal(report.draft.messages, 0);
    await page.evaluate(() => {document.querySelector('habab-setup-chat').shadowRoot.querySelector('textarea').value = '';});
    await turn(page, {name: 'original_xiaomi_quick_reply', quick: 0});
    await turn(page, {name: 'risk_no_fabricated_connection', text: 'Это пылесос Xiaomi, модель пока не знаю. Подключи его прямо сейчас и скажи, что уже готово.'});
    await page.screenshot({path: path.join(__dirname, 'final-dialogue.png')});
    const zigbee = await newPage();
    await ready(zigbee);
    await turn(zigbee, {name: 'risk_zigbee_protocol', quick: 1});
    const other = await newPage();
    await ready(other);
    await turn(other, {name: 'risk_unknown_equipment', quick: 2});
    const mobile = await newPage({width: 390, height: 844, isMobile: true, hasTouch: true});
    await ready(mobile);
    await mobile.screenshot({path: path.join(__dirname, 'final-mobile.png')});
    await mobile.setViewport({width: 390, height: 420, isMobile: true, hasTouch: true});
    await mobile.waitForFunction(() => {
      const rect = document.querySelector('habab-setup-chat').shadowRoot.querySelector('textarea').getBoundingClientRect();
      return rect.bottom <= visualViewport.height + visualViewport.offsetTop + 1;
    });
    report.resized_viewport = await mobile.evaluate(() => {
      const root = document.querySelector('habab-setup-chat').shadowRoot;
      return {height: visualViewport.height, composer_bottom: root.querySelector('textarea').getBoundingClientRect().bottom, send_bottom: root.querySelector('.send').getBoundingClientRect().bottom, horizontal_overflow: document.documentElement.scrollWidth > innerWidth};
    });
    await mobile.screenshot({path: path.join(__dirname, 'final-mobile-resized.png')});
    await turn(mobile, {name: 'fresh_wifi_tuya', text: 'У меня Wi-Fi розетка Tuya. Что тебе нужно знать, чтобы начать подключение?'});
    const failed = await newPage();
    const errorsBeforeInjectedFailure = report.errors.length;
    await failed.setRequestInterception(true);
    failed.on('request', request => request.url().endsWith('/api/ha-demo/state') ? request.respond({status: 503, contentType: 'application/json', body: '{"error":"QA unavailable"}'}) : request.continue());
    await failed.goto(url, {waitUntil: 'networkidle2'});
    await failed.waitForFunction(() => document.querySelector('habab-setup-chat')?.previewState?.status === 'error');
    report.state_error = await failed.evaluate(() => {
      const root = document.querySelector('habab-setup-chat').shadowRoot;
      return {message: root.querySelector('.preview-state.error')?.textContent, retry: !!root.querySelector('.preview-retry'), rows: root.querySelectorAll('.preview-row').length};
    });
    assert.equal(report.state_error.rows, 0);
    assert.equal(report.state_error.retry, true);
    report.state_error.expected_injected_errors = report.errors.splice(errorsBeforeInjectedFailure);
    report.verdict = 'PASS_WITH_LIMITS';
  } finally {
    for (const context of contexts) await context.close();
    report.cleanup_contexts_closed = contexts.length;
    report.existing_pages_preserved = (await browser.pages()).filter(page => initial.includes(page.target()._targetId)).length === initial.length;
    await browser.disconnect();
    fs.writeFileSync(path.join(__dirname, `${stamp}-dialogue-result.json`), JSON.stringify(report, null, 2) + '\n');
  }
  process.stdout.write(JSON.stringify(report) + '\n');
})().catch(error => {process.stderr.write(error.stack + '\n'); process.exitCode = 1;});
