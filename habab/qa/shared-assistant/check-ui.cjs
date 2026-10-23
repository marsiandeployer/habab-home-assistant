const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const puppeteer=require('../../node_modules/puppeteer-core');
const privateMode=process.env.HABAB_QA_MODE==='private';
const base=privateMode?'http://127.0.0.1:8403':'https://habab-demo.wpmix.net';
const report={base,private_source_overlay:false,private_baked_image:privateMode?'sha256:9efca5c859862be84723b9ce299df0b41be43377e2b9e1c3a53c7699645fe8d1':undefined,errors:[],cases:[],limitations:['Mobile keyboard space is emulated by viewport resize.']};
const suffix=privateMode?'private':'public';

async function ready(page){
  await page.waitForFunction(()=>{
    const chat=document.querySelector('habab-setup-chat');
    const find=root=>{for(const n of root.querySelectorAll('*')){if(n.localName==='home-assistant-main')return n;if(n.shadowRoot){const x=find(n.shadowRoot);if(x)return x;}}};
    return chat?.sessionId&&!chat.busy&&chat.shadowRoot.querySelector('dialog').open&&!!find(document)?.hass&&!document.querySelector('#ha-launch-screen');
  },{timeout:45000});
  await page.evaluate(async()=>{await Promise.all(document.querySelector('habab-setup-chat').shadowRoot.querySelector('dialog').getAnimations().map(a=>a.finished.catch(()=>{})));});
  await page.waitForNetworkIdle({idleTime:500,concurrency:0,timeout:20000});
}
async function state(page){return page.evaluate(()=>{
  const chat=document.querySelector('habab-setup-chat'),root=chat.shadowRoot;
  const box=node=>{const b=node.getBoundingClientRect();return{x:b.x,y:b.y,right:b.right,bottom:b.bottom,width:b.width,height:b.height};};
  const dashboard=document.querySelector('body > ha-demo,body > home-assistant');
  return{width:innerWidth,height:innerHeight,open:root.querySelector('dialog').open,chat:box(root.querySelector('dialog')),dashboard:box(dashboard),inert:dashboard.inert,input:box(root.querySelector('textarea')),send:box(root.querySelector('.send')),expanded:chat.mobileExpanded,duplicate_rows:root.querySelectorAll('.preview-dashboard,.preview-row').length,status:root.querySelector('#status').textContent,hook:!!window.hababAssistant,scrollbar:document.documentElement.scrollWidth>innerWidth};
});}
async function click(page,selector){const h=await page.evaluateHandle(selector=>document.querySelector('habab-setup-chat').shadowRoot.querySelector(selector),selector);await h.asElement().click();await h.dispose();await page.evaluate(async()=>{await Promise.all(document.querySelector('habab-setup-chat').shadowRoot.querySelector('dialog').getAnimations().map(a=>a.finished.catch(()=>{})));});}

(async()=>{
  const browser=await puppeteer.connect({browserURL:'http://127.0.0.1:9222'}),context=await browser.createBrowserContext();
  const initial=(await browser.pages()).filter(p=>p.browserContext()!==context).map(p=>p.target()._targetId);
  try{
    const page=await context.newPage();page.on('pageerror',e=>report.errors.push(e.message));
    report.failed_requests=[];
    page.on('requestfailed',r=>report.failed_requests.push({url:r.url(),reason:r.failure()?.errorText,type:r.resourceType()}));
    if(privateMode){
      assert.ok(process.env.HABAB_QA_PRIVATE);
      const tokens=JSON.parse(fs.readFileSync(path.join(process.env.HABAB_QA_PRIVATE,'tokens.json'),'utf8'));
      await page.evaluateOnNewDocument(tokens=>{if(location.origin==='http://127.0.0.1:8403'){localStorage.setItem('hassTokens',JSON.stringify({...tokens,hassUrl:tokens.base_url,clientId:tokens.client_id,expires:Date.now()+tokens.expires_in*1000}));localStorage.setItem('selectedLanguage',JSON.stringify('ru'));}},tokens);
    }
    for(const route of privateMode?['/config/dashboard','/habab-ai','/lovelace/0']:['/','/?habab-preview=onboarding','/demos/home-assistant/']){
      await page.setViewport({width:1280,height:800});
      await page.goto(base+route,{waitUntil:'networkidle2',timeout:45000});await ready(page);
      const s=await state(page);report.cases.push({route,view:'desktop',...s});
      assert.equal(s.duplicate_rows,0);assert.ok(s.dashboard.right<=s.chat.x+1);assert.equal(s.open,true);assert.equal(s.hook,privateMode);
      await page.screenshot({path:path.join(__dirname,`${suffix}-desktop.png`)});
      if(!report.stock_assist){
        report.stock_assist=await page.evaluate(async()=>{
          const find=root=>{for(const n of root.querySelectorAll('*')){if(n.localName==='home-assistant-main')return n;if(n.shadowRoot){const x=find(n.shadowRoot);if(x)return x;}}};
          const before=document.querySelector('habab-setup-chat');let imports=0;
          const event=new CustomEvent('show-dialog',{bubbles:true,composed:true,cancelable:true,detail:{dialogTag:'ha-voice-command-dialog',dialogParams:{start_listening:false},dialogImport:async()=>{imports++;}}});
          find(document).dispatchEvent(event);await Promise.resolve();
          return{same_widget:document.querySelector('habab-setup-chat')===before,widgets:document.querySelectorAll('habab-setup-chat').length,stock_imports:imports,prevented:event.defaultPrevented};
        });
        assert.equal(report.stock_assist.same_widget,true);assert.equal(report.stock_assist.widgets,1);assert.equal(report.stock_assist.stock_imports,0);assert.equal(report.stock_assist.prevented,true);
      }
    }
    if(!privateMode){
      report.compatibility_entry=await page.evaluate(async()=>{
        const before=document.querySelector('habab-setup-chat');
        await new Promise((resolve,reject)=>{const script=document.createElement('script');script.type='module';script.src='/demos/home-assistant/integrations/setup-chat.js?v=5';script.onload=resolve;script.onerror=reject;document.head.append(script);});
        return{same_widget:document.querySelector('habab-setup-chat')===before,widgets:document.querySelectorAll('habab-setup-chat').length};
      });
      assert.equal(report.compatibility_entry.same_widget,true);assert.equal(report.compatibility_entry.widgets,1);
    }
    await page.setViewport({width:390,height:844,isMobile:true,hasTouch:true});await ready(page);
    let s=await state(page);report.cases.push({view:'mobile-stacked',...s});
    assert.ok(Math.abs(s.chat.y-422)<2);assert.ok(s.dashboard.bottom<=s.chat.y+1);assert.equal(s.expanded,false);assert.equal(s.inert,false);assert.ok(s.input.bottom<=844);
    await page.screenshot({path:path.join(__dirname,`${suffix}-mobile-stacked.png`)});
    await click(page,'textarea');await page.waitForFunction(()=>document.querySelector('habab-setup-chat').mobileExpanded);
    const input=await page.evaluateHandle(()=>document.querySelector('habab-setup-chat').shadowRoot.querySelector('textarea'));
    await input.asElement().type('Черновик для проверки');await input.dispose();
    s=await state(page);report.cases.push({view:'mobile-expanded',...s});assert.ok(s.chat.y<2);assert.equal(s.inert,true);
    await page.screenshot({path:path.join(__dirname,`${suffix}-mobile-expanded.png`)});
    await page.setViewport({width:390,height:420,isMobile:true,hasTouch:true});
    await page.waitForFunction(()=>document.querySelector('habab-setup-chat').shadowRoot.querySelector('textarea').getBoundingClientRect().bottom<=420);
    s=await state(page);report.cases.push({view:'mobile-keyboard-space',...s});assert.ok(s.input.bottom<=420);assert.ok(s.chat.y<2);
    await page.screenshot({path:path.join(__dirname,`${suffix}-mobile-keyboard-space.png`)});
    await page.setViewport({width:390,height:844,isMobile:true,hasTouch:true});await click(page,'.collapse');
    s=await state(page);report.cases.push({view:'mobile-return-to-dashboard',...s});assert.equal(s.expanded,false);assert.equal(s.inert,false);assert.ok(Math.abs(s.chat.y-422)<2);
    report.draft_preserved=await page.evaluate(()=>document.querySelector('habab-setup-chat').shadowRoot.querySelector('textarea').value==='Черновик для проверки');assert.equal(report.draft_preserved,true);
    await click(page,'.close');await new Promise(resolve=>setTimeout(resolve,1100));
    report.closed_respected=await page.evaluate(()=>!document.querySelector('habab-setup-chat').shadowRoot.querySelector('dialog').open);assert.equal(report.closed_respected,true);
    await click(page,'.launcher');s=await state(page);assert.equal(s.open,true);assert.equal(s.expanded,false);assert.ok(Math.abs(s.chat.y-422)<2);
    assert.equal(report.errors.length,0);report.verdict='PASS_WITH_LIMITS';
  }finally{
    await context.close();report.context_closed=true;
    report.existing_pages_preserved=(await browser.pages()).filter(p=>initial.includes(p.target()._targetId)).length===initial.length;
    await browser.disconnect();fs.writeFileSync(path.join(__dirname,`${suffix}-ui.json`),JSON.stringify(report,null,2)+'\n');
  }
  process.stdout.write(JSON.stringify(report)+'\n');
})().catch(e=>{process.stderr.write(e.stack+'\n');process.exitCode=1;});
