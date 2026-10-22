const fs=require('fs'),path=require('path'),assert=require('assert/strict'),puppeteer=require('puppeteer-core');
(async()=>{
  const profile=fs.mkdtempSync('/tmp/codex-chrome-run-ha-telegram-');
  const report={checkedAt:new Date().toISOString(),cases:[]};let browser;
  try{
    browser=await puppeteer.launch({executablePath:'/snap/bin/chromium',headless:true,userDataDir:profile,args:['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage']});
    for(const[mode,width,height]of[['desktop',1280,800],['mobile',390,844]]){
      const context=await browser.createBrowserContext();
      try{
        const page=await context.newPage();await page.setViewport({width,height,isMobile:mode==='mobile',hasTouch:mode==='mobile'});
        const fixture=['123456789','x'.repeat(35)].join(':');const errors=[];let tokenSent=false,telegramRequest=false;
        page.on('request',request=>{if((request.postData()||'').includes(fixture)||request.url().includes(fixture))tokenSent=true;if(request.url().includes('api.telegram.org'))telegramRequest=true;});
        page.on('pageerror',error=>errors.push(error.message));
        const response=await page.goto('https://habab-demo.wpmix.net/',{waitUntil:'networkidle2',timeout:45000});assert.equal(response.status(),200);
        await page.waitForFunction(()=>!!document.querySelector('ha-demo')?.hass?.states?.['light.living_room_floor_lamp']);
        await page.waitForFunction(()=>{function walk(root){if([...root.querySelectorAll('ha-card')].some(el=>el.innerText?.includes('Торшер')&&el.getBoundingClientRect().width>0))return true;return [...root.querySelectorAll('*')].some(el=>el.shadowRoot&&walk(el.shadowRoot));}return walk(document);});
        await page.evaluate(async()=>await document.fonts.ready);
        const el=async selector=>(await page.evaluateHandle(selector=>document.querySelector('habab-telegram-setup').shadowRoot.querySelector(selector),selector)).asElement();
        const read=async()=>page.evaluate(()=>{const r=document.querySelector('habab-telegram-setup').shadowRoot;const box=r.querySelector('dialog').getBoundingClientRect();return{open:r.querySelector('dialog').open,password:r.querySelector('input').type==='password',empty:r.querySelector('input').value==='',invalid:r.querySelector('input').getAttribute('aria-invalid')==='true',errorVisible:!r.querySelector('.error').hidden,success:!r.querySelector('.success').hidden,inside:box.left>=0&&box.right<=innerWidth&&box.top>=0&&box.bottom<=innerHeight,overflow:document.documentElement.scrollWidth>innerWidth};});
        await(await el('.launcher')).click();assert((await read()).password&&(await read()).inside);
        await page.screenshot({path:path.join(__dirname,`${mode}-telegram.png`)});
        await(await el('input')).type('invalid');await(await el('button[type=submit]')).click();assert((await read()).invalid&&(await read()).errorVisible);
        await(await el('.cancel')).click();assert((await read()).empty&&!((await read()).open));
        await(await el('.launcher')).click();await(await el('input')).type(fixture);
        await(await el('.reveal')).click();assert(!(await read()).password);await(await el('.reveal')).click();
        await(await el('button[type=submit]')).click();assert((await read()).success&&(await read()).empty);
        const stored=await page.evaluate(token=>[localStorage,sessionStorage].some(storage=>Object.values(storage).some(value=>String(value).includes(token))),fixture);assert(!stored&&!tokenSent&&!telegramRequest);
        await page.screenshot({path:path.join(__dirname,`${mode}-telegram-success.png`)});
        await(await el('.close')).click();await(await el('.launcher')).click();await page.keyboard.press('Escape');assert(!(await read()).open&&(await read()).empty);
        assert.deepEqual(errors,[]);assert(!(await read()).overflow);
        report.cases.push({mode,passed:true,tokenNeverSent:true,tokenNeverStored:true,tokenCleared:true,invalidTokenHandled:true,showHideWorks:true,escapeCloses:true,dialogFits:true,errors});
      }finally{await context.close();}
    }report.passed=true;
  }catch(error){report.passed=false;report.error=error.stack;process.exitCode=1;}
  finally{if(browser)await browser.close();fs.rmSync(profile,{recursive:true,force:true});const snap=path.join('/tmp/snap-private-tmp/snap.chromium/tmp',path.basename(profile));if(fs.existsSync(snap)&&!fs.lstatSync(snap).isSymbolicLink())fs.rmSync(snap,{recursive:true,force:true});fs.writeFileSync(path.join(__dirname,'telegram-browser.json'),JSON.stringify(report,null,2)+'\n');}
  console.log(JSON.stringify(report));
})();
