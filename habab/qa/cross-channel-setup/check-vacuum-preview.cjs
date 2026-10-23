const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {connect}=require('/root/kolya/chrome_tunnel_client.cjs');
const puppeteer=require('../../node_modules/puppeteer-core');
(async()=>{
 const local=process.env.HABAB_VACUUM_QA_MODE==='local',suffix=local?'local':'windows',base=local?'http://127.0.0.1:8403':'http://127.0.0.1:8123';
 const r={recorded_at_utc:new Date().toISOString(),scope:local?'Isolated Linux baked0.3.1 native card render fixture':'User Windows read-only assets and browser-only vacuum fixture',fixture_is_physical_device:false,backend_configuration_writes:0};let c;
 try{
  r.stage='connect';if(local){const browser=await puppeteer.connect({browserURL:'http://127.0.0.1:9222'});const context=await browser.createBrowserContext();c={newPage:()=>context.newPage(),run:(_p,op)=>op(),close:async()=>{await context.close();await browser.disconnect();}};}else c=await connect({puppeteer});r.stage='navigate';const p=await c.newPage();if(local){const t=JSON.parse(fs.readFileSync('/root/.local/share/habab-canonical-qa/tokens.json','utf8'));await p.evaluateOnNewDocument(t=>{localStorage.setItem('hassTokens',JSON.stringify({...t,hassUrl:t.base_url,clientId:t.client_id,expires:Date.now()+t.expires_in*1000}));localStorage.setItem('selectedLanguage',JSON.stringify('ru'));},t);}
  await c.run(p,()=>p.goto(base+'/lovelace/0',{waitUntil:'networkidle2',timeout:30000}));
  await c.run(p,()=>p.waitForFunction(()=>document.querySelector('habab-setup-chat')?.hass?.user?.is_admin,{timeout:25000}));
  r.stage='inspect_assets_and_fixture';r.preview=await c.run(p,()=>p.evaluate(async()=>{
   const h=document.querySelector('habab-setup-chat').hass;
   const digest=async buffer=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',buffer))).map(v=>v.toString(16).padStart(2,'0')).join('');
   const moduleResponse=await fetch('/habab-home/device-dashboard.js'),imageResponse=await fetch('/habab-home/assets/robot-vacuum.svg');
   const moduleBytes=await moduleResponse.arrayBuffer(),imageBytes=await imageResponse.arrayBuffer();
   const assets={module_status:moduleResponse.status,module_sha256:await digest(moduleBytes),image_status:imageResponse.status,image_type:imageResponse.headers.get('content-type'),image_sha256:await digest(imageBytes)};
   if(!moduleResponse.ok||!imageResponse.ok)return{assets,rendered:false,reason:'asset_unavailable'};
   if(typeof window.loadCardHelpers!=='function')return{assets,rendered:false,reason:'native_card_factory_unavailable'};
   const {buildVacuumCards}=await import('/habab-home/device-dashboard.js');
   const helpers=await window.loadCardHelpers();
   const now=new Date().toISOString();
   const states={
    'vacuum.habab_visual_fixture':{entity_id:'vacuum.habab_visual_fixture',state:'docked',attributes:{friendly_name:'Робот-пылесос',supported_features:8192|4|8|16},last_changed:now,last_updated:now,context:{id:'visual-fixture',user_id:null,parent_id:null}},
    'sensor.habab_visual_battery':{entity_id:'sensor.habab_visual_battery',state:'86',attributes:{friendly_name:'Заряд батареи',device_class:'battery',unit_of_measurement:'%'},last_changed:now,last_updated:now,context:{id:'visual-fixture',user_id:null,parent_id:null}},
   };
   const entries=[{entity_id:'vacuum.habab_visual_fixture',device_id:'visual-fixture',original_name:'Робот-пылесос'},{entity_id:'sensor.habab_visual_battery',device_id:'visual-fixture',device_class:'battery',original_name:'Заряд батареи'}];
   const devices=[{id:'visual-fixture',manufacturer:'Xiaomi',model:'Robot Vacuum S10',name:'Робот-пылесос S10'}];
   const fixtureHass={...h,states:{...h.states,...states},callService:()=>{throw new Error('Actuation disabled in visual fixture');}};
   const configs=buildVacuumCards(states,entries,devices,'ru');
   const host=document.createElement('section');host.id='habab-vacuum-visual-fixture';host.style.cssText='position:fixed;inset:12px auto auto 12px;width:390px;max-width:calc(100vw - 24px);padding:16px;box-sizing:border-box;background:#fafafa;color:#202124;z-index:2147483647;border-radius:12px;display:grid;gap:12px;font:14px Roboto,sans-serif;';
   const label=document.createElement('p');label.textContent='Предпросмотр карточки. Тестовые данные; пылесос не подключён.';label.style.margin='0';host.append(label);
   const context=document.createElement('habab-native-form-context');context.hass=fixtureHass;context.style.cssText='display:grid;gap:12px;min-width:0';host.append(context);for(const config of configs){const card=helpers.createCardElement(config);card.hass=fixtureHass;context.append(card);}
   document.body.append(host);
   await Promise.all([...context.children].map(x=>x.updateComplete));
   await Promise.all(['hui-picture-entity-card','hui-tile-card'].map(tag=>customElements.whenDefined(tag)));await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));return{assets,rendered:true,card_types:configs.map(c=>c.type),card_count:configs.length,virtual_ids_in_native_backend:Object.keys(h.states).filter(k=>k.includes('habab_visual_')).length};
  }));
  r.local_source_sha256=crypto.createHash('sha256').update(fs.readFileSync(path.join(__dirname,'../../distribution/src/habab_home/frontend/device-dashboard.js'))).digest('hex');
  r.local_image_sha256=crypto.createHash('sha256').update(fs.readFileSync(path.join(__dirname,'../../distribution/src/habab_home/frontend/assets/robot-vacuum.svg'))).digest('hex');
  r.installed_source_matches=r.preview.assets.module_sha256===r.local_source_sha256;
  r.installed_image_matches=r.preview.assets.image_sha256===r.local_image_sha256;
  if(r.preview.rendered){await c.run(p,()=>p.waitForFunction(()=>{const h=document.querySelector('#habab-vacuum-visual-fixture');return h&&h.getBoundingClientRect().height>350;}));await c.run(p,()=>p.$('#habab-vacuum-visual-fixture').then(async el=>{try{return await el.screenshot({path:__dirname+'/'+suffix+'-vacuum-preview.png'});}finally{await el.dispose();}}));}
  r.verdict=r.installed_source_matches&&r.installed_image_matches?'INSTALLED_ASSETS_MATCH':'ASSET_MISMATCH';
 }catch(e){r.verdict='CHECK_FAILED';r.error=/timeout/i.test(e.message)?'timeout':e.code||e.name;r.reason=String(e.message).replace(/(?:https?|wss?):\/\/[^\s]+/g,'[URL]').slice(0,300);}
 finally{if(c){await c.close();r.own_tab_closed=true;}fs.writeFileSync(__dirname+'/'+suffix+'-vacuum-preview.json',JSON.stringify(r,null,2)+'\n');}
 process.stdout.write(JSON.stringify(r)+'\n');
})();
