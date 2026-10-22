const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const puppeteer = require('puppeteer-core');
(async () => {
  const profile = fs.mkdtempSync('/tmp/codex-chrome-run-ha-move-');
  let browser;
  const report = {checkedAt: new Date().toISOString(), cases: []};
  try {
    browser = await puppeteer.launch({executablePath: '/snap/bin/chromium', headless: true,
      userDataDir: profile, args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']});
    for (const [mode,width,height] of [['desktop',1280,800],['mobile',390,844]]) {
      const context = await browser.createBrowserContext();
      try {
        const page = await context.newPage();
        await page.setViewport({width,height,isMobile:mode==='mobile',hasTouch:mode==='mobile'});
        const errors=[];
        page.on('pageerror', error => errors.push(error.message));
        async function loaded() {
          await page.waitForFunction(() => !!document.querySelector('ha-demo')?.hass?.states?.['light.living_room_floor_lamp']);
          await page.waitForFunction(() => {
            function walk(root) {
              if ([...root.querySelectorAll('ha-card')].some(el => el.innerText?.includes('Торшер') && el.getBoundingClientRect().width > 0)) return true;
              return [...root.querySelectorAll('*')].some(el => el.shadowRoot && walk(el.shadowRoot));
            }
            return walk(document);
          });
        }
        const response=await page.goto('https://habab-demo.wpmix.net/',{waitUntil:'networkidle2',timeout:45000});
        assert.equal(response.status(),200);
        await loaded();
        const saved = page.waitForResponse(response => response.url().endsWith('/api/ha-demo/state') && response.request().method()==='POST' && response.status()===200);
        await page.evaluate(async () => {
          await document.querySelector('ha-demo').hass.callService('light','turn_off',{entity_id:'light.living_room_floor_lamp'});
        });
        await saved;
        await page.reload({waitUntil:'networkidle2',timeout:45000});
        await loaded();
        const result=await page.evaluate(() => ({
          release:document.querySelector('meta[name="habab-demo-release"]').content,
          state:document.querySelector('ha-demo').hass.states['light.living_room_floor_lamp'].state,
          language:document.querySelector('ha-demo').hass.locale.language,
          overflow:document.documentElement.scrollWidth>innerWidth,
        }));
        assert.equal(result.release,'20261021-ui5');
        assert.equal(result.state,'off');
        assert.equal(result.language,'ru');
        assert.equal(result.overflow,false);
        assert.deepEqual(errors,[]);
        await page.screenshot({path:path.join(__dirname,`${mode}.png`)});
        report.cases.push({mode,...result,errors,persistenceAfterReload:true,nativeCardsRendered:true,passed:true});
      } finally {await context.close();}
    }
    report.passed=true;
  } catch(error) {report.passed=false;report.error=error.stack;process.exitCode=1;
  } finally {
    if(browser)await browser.close();
    fs.rmSync(profile,{recursive:true,force:true});
    const snap=path.join('/tmp/snap-private-tmp/snap.chromium/tmp',path.basename(profile));
    if(fs.existsSync(snap)&&!fs.lstatSync(snap).isSymbolicLink())fs.rmSync(snap,{recursive:true,force:true});
    fs.writeFileSync(path.join(__dirname,'browser-migration.json'),JSON.stringify(report,null,2)+'\n');
  }
  console.log(JSON.stringify(report));
})();
