const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const puppeteer = require('puppeteer-core');
const runtime = process.env.HABAB_QA_RUNTIME;
if (!runtime) throw new Error('HABAB_QA_RUNTIME is required');
const auth = JSON.parse(fs.readFileSync(path.join(runtime, 'qa-auth.json')));
const base = 'http://127.0.0.1:8395';
const output = path.join(__dirname, 'setup-agent');
const find = () => {
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      // Scoped HA registries rewrite tag selectors; localName is stable.
      if (el.localName === 'habab-setup-chat') return el;
      if (el.shadowRoot) {const result = walk(el.shadowRoot); if (result) return result;}
    }
  };
  return walk(document);
};
(async () => {
  const refreshed = await fetch(base + '/auth/token', {method:'POST', body:new URLSearchParams({
    grant_type:'refresh_token', refresh_token:auth.refresh_token, client_id:base+'/',
  })});
  assert(refreshed.ok, 'QA auth refresh failed');
  Object.assign(auth, await refreshed.json());
  fs.writeFileSync(path.join(runtime,'qa-auth.json'), JSON.stringify(auth), {mode:0o600});
  const browser = await puppeteer.launch({executablePath:'/snap/bin/chromium', headless:true,
    args:['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage']});
  const report = {cases:[], errors:[]};
  try {
    for (const mode of ['native','demo']) {
      const context = await browser.createBrowserContext();
      const page = await context.newPage();
      page.on('pageerror', error => report.errors.push({mode, message:error.message}));
      await page.setViewport({width:1280,height:800});
      if (mode==='native') await page.evaluateOnNewDocument((tokens,url) => {
        localStorage.setItem('hassTokens',JSON.stringify({...tokens,hassUrl:url,clientId:url+'/',expires:Date.now()+tokens.expires_in*1000}));
        localStorage.setItem('selectedLanguage',JSON.stringify('ru'));
      },auth,base);
      await page.goto(mode==='native'?base+'/lovelace/0':'https://habab-demo.wpmix.net/',{waitUntil:'networkidle2',timeout:45000});
      await page.waitForFunction(find,{timeout:30000});
      await page.waitForFunction(() => !document.querySelector('#ha-launch-screen'),{timeout:30000});
      await page.evaluateHandle(find).then(handle => page.evaluate(el => {window.qaChat=el;},handle));
      assert(await page.evaluate(() => window.qaChat.parentElement.classList.contains('action-items')));
      await page.screenshot({path:path.join(output,`${mode}-desktop-icon.png`)});
      await page.evaluate(() => window.qaChat.q('.launcher').click());
      await page.waitForFunction(() => window.qaChat.sessionId && !window.qaChat.busy,{timeout:45000});
      assert(await page.evaluate(() => !window.qaChat.q('textarea').disabled));
      await page.waitForFunction(() => !window.qaChat.q('dialog').getAnimations().some(a => a.playState==='running'));
      await page.evaluate(async () => {await Promise.allSettled([...window.qaChat.shadowRoot.querySelectorAll('*')].filter(e=>e.localName==='ha-markdown').map(e=>e.updateComplete));});
      await page.screenshot({path:path.join(output,`${mode}-desktop-chat.png`)});
      if (mode==='native') {
        await page.evaluate(() => {window.qaChat.q('textarea').value='Проверь реальной командой содержимое /config/habab-agent-check.txt и ответь одной короткой фразой.'; window.qaChat.q('form').requestSubmit();});
        await page.waitForFunction(() => !window.qaChat.busy,{timeout:200000});
        assert(await page.evaluate(() => window.qaChat.messages.at(-1).role==='assistant' && !window.qaChat.q('#status').classList.contains('error')));
        await page.evaluate(async () => {await Promise.allSettled([...window.qaChat.shadowRoot.querySelectorAll('*')].filter(e=>e.localName==='ha-markdown').map(e=>e.updateComplete));});
        await page.screenshot({path:path.join(output,'native-desktop-result.png')});
      } else {
        const input = (await page.evaluateHandle(() => window.qaChat.q('textarea'))).asElement();
        await input.type('Как подключить датчик температуры по Bluetooth к Home Assistant? Ответь кратко.');
        await page.keyboard.press('Enter');
        await page.waitForFunction(() => !window.qaChat.busy,{timeout:40000});
        assert(await page.evaluate(() => window.qaChat.messages.at(-1).role==='assistant' && !window.qaChat.q('#status').classList.contains('error')));
        await page.evaluate(async () => {await Promise.allSettled([...window.qaChat.shadowRoot.querySelectorAll('*')].filter(e=>e.localName==='ha-markdown').map(e=>e.updateComplete));});
        await page.screenshot({path:path.join(output,'demo-desktop-result.png')});
      }
      await page.setViewport({width:390,height:844,isMobile:true,hasTouch:true});
      await page.waitForFunction(find,{timeout:30000});
      await page.evaluateHandle(find).then(handle => page.evaluate(el => {window.qaChat=el;},handle));
      if (!await page.evaluate(() => window.qaChat.q('dialog').open)) {
        await page.evaluate(() => window.qaChat.q('.launcher').click());
        await page.waitForFunction(() => window.qaChat.sessionId && !window.qaChat.busy,{timeout:45000});
      }
      await page.waitForFunction(() => !window.qaChat.q('dialog').getAnimations().some(a => a.playState==='running'));
      await page.evaluate(async () => {await Promise.allSettled([...window.qaChat.shadowRoot.querySelectorAll('*')].filter(e=>e.localName==='ha-markdown').map(e=>e.updateComplete));});
      const dimensions=await page.evaluate(() => {
        const c=window.qaChat,dialog=c.q('dialog').getBoundingClientRect();
        return {width:innerWidth, dialogWidth:dialog.width,overflow:c.q('dialog').scrollWidth>dialog.width || c.q('#log').scrollWidth>c.q('#log').clientWidth};
      });
      assert(dimensions.dialogWidth<=dimensions.width && !dimensions.overflow);
      await page.screenshot({path:path.join(output,`${mode}-mobile-chat.png`)});
      await page.keyboard.press('Escape');
      assert(await page.evaluate(() => !window.qaChat.q('dialog').open));
      await page.evaluate(() => window.qaChat.q('.launcher').click());
      assert(await page.evaluate(() => window.qaChat.q('dialog').open));
      report.cases.push({mode,pass:true,viewports:['1280x800','390x844'],toolbar:true,escapeAndReopen:true});
      await context.close();
    }
    assert.deepEqual(report.errors,[]);
    report.status='PASS';
  } finally {
    fs.writeFileSync(path.join(output,'browser.json'),JSON.stringify(report,null,2)+'\n');
    await browser.close();
  }
  console.log(JSON.stringify(report));
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
