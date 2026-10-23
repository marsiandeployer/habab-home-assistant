const fs=require('node:fs');
const {connect}=require('/root/kolya/chrome_tunnel_client.cjs');
const puppeteer=require('../../node_modules/puppeteer-core');
(async()=>{
 let client;
 const report={scope:'Read-only user Windows browser; own background tab only',configuration_writes:0};
 try{
  client=await connect({puppeteer});report.profile_mode=client.profileMode;
  const page=await client.newPage();
  await client.run(page,()=>page.goto('http://127.0.0.1:8123/',{waitUntil:'domcontentloaded',timeout:20000}));
  await client.run(page,()=>page.waitForFunction(()=>document.querySelector('home-assistant')?.shadowRoot||location.pathname.startsWith('/auth'),{timeout:20000}));
  await client.run(page,()=>page.waitForFunction(()=>{const find=r=>{for(const n of r.querySelectorAll('*')){if(n.localName==='home-assistant-main')return n;if(n.shadowRoot){const x=find(n.shadowRoot);if(x)return x;}}};const m=find(document);return m?.hass?.states&&Object.keys(m.hass.states).length>0;},{timeout:25000}));
  report.state=await client.run(page,()=>page.evaluate(async()=>{
   const find=root=>{for(const n of root.querySelectorAll('*')){if(n.localName==='home-assistant-main')return n;if(n.shadowRoot){const v=find(n.shadowRoot);if(v)return v;}}};
   const main=find(document),hass=main?.hass,widget=document.querySelector('habab-setup-chat');
   const status=hass?await hass.callApi('GET','habab_home/status').catch(()=>null):null;
   const devices=hass?await hass.callWS({type:'config/device_registry/list'}).catch(()=>[]):[];
   const entities=hass?await hass.callWS({type:'config/entity_registry/list'}).catch(()=>[]):[];
   const resources=performance.getEntriesByType('resource').map(x=>new URL(x.name).pathname).filter(p=>/setup-chat|habab-assistant/.test(p));
   return {registered_device_count:devices.length,registered_vacuum_count:entities.filter(e=>e.entity_id.startsWith('vacuum.')).length,product_version:status?.version||null,assistant_modules:[...new Set(resources)],states_count:Object.keys(hass?.states||{}).length,route:location.pathname,authenticated:!!hass?.user,admin:!!hass?.user?.is_admin,core_version:hass?.config?.version||null,assistant_widgets:document.querySelectorAll('habab-setup-chat').length,assistant_open:!!widget?.shadowRoot?.querySelector('dialog')?.open,vacuum_entities:Object.keys(hass?.states||{}).filter(k=>k.startsWith('vacuum.')).length};
  }));report.verdict=report.state.authenticated?'READ_ONLY_ACCESS_CONFIRMED':'LOGIN_REQUIRED';
 }catch(e){report.verdict='ACCESS_UNAVAILABLE';report.error=/approval|timeout/i.test(e.message)?'connection_or_page_timeout':/Chrome connection|ECONNREFUSED/i.test(e.message)?'chrome_tunnel_unavailable':'connection_or_navigation_failed';}
 finally{if(client){await client.close();report.own_tabs_closed=true;}fs.writeFileSync(__dirname+'/windows-access.json',JSON.stringify(report,null,2)+'\n');}
 process.stdout.write(JSON.stringify(report)+'\n');
})();
