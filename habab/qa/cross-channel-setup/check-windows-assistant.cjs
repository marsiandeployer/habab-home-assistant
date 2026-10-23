const fs=require('node:fs');
const {connect}=require('/root/kolya/chrome_tunnel_client.cjs');
const puppeteer=require('../../node_modules/puppeteer-core');
(async()=>{
 const r={scope:'Read-only assistant request in own user Windows background tab',settings_or_device_writes_requested:false};let c;
 try{
  c=await connect({puppeteer});const p=await c.newPage();
  await c.run(p,()=>p.goto('http://127.0.0.1:8123/',{waitUntil:'networkidle2',timeout:30000}));
  await c.run(p,()=>p.waitForFunction(()=>document.querySelector('habab-setup-chat')?.hass?.user?.is_admin,{timeout:25000}));
  r.inventory=await c.run(p,()=>p.evaluate(async()=>{
   const w=document.querySelector('habab-setup-chat'),h=w.hass;
   const entries=await h.callWS({type:'config_entries/get'});
   return {states:Object.keys(h.states).length,vacuums:Object.keys(h.states).filter(k=>k.startsWith('vacuum.')).length,integration_domains:entries.map(e=>e.domain).sort()};
  }));
  await c.run(p,()=>p.evaluate(()=>document.querySelector('habab-setup-chat').shadowRoot.querySelector('.launcher').click()));
  await c.run(p,()=>p.waitForFunction(()=>{const w=document.querySelector('habab-setup-chat');return !w.busy;},{timeout:45000}));
  const start=await c.run(p,()=>p.evaluate(()=>{const w=document.querySelector('habab-setup-chat');return{session_ready:!!w.sessionId,status:w.shadowRoot.querySelector('#status')?.textContent};}));r.start=start;
  if(start.session_ready){
   await c.run(p,()=>p.evaluate(()=>{
    const w=document.querySelector('habab-setup-chat');w.shadowRoot.querySelector('textarea').value='Проверь реальные интеграции и устройства через доступные команды. Ничего не изменяй, не подключай, не запускай и не выключай. Скажи кратко: есть ли подключённый пылесос?';w.shadowRoot.querySelector('.send').click();
   }));
   await c.run(p,()=>p.waitForFunction(()=>!document.querySelector('habab-setup-chat').busy,{timeout:120000}));
   r.turn=await c.run(p,()=>p.evaluate(async()=>{
    const w=document.querySelector('habab-setup-chat');let diag=null;
    try{const d=await w.hass.callWS({type:'habab_home/agent/diagnostics',session_id:w.sessionId});diag={active:d.active,runtime_status:d.runtime_status,error:d.error};}catch{}
    return{status:w.shadowRoot.querySelector('#status')?.textContent,last_role:w.messages?.at(-1)?.role,answer:w.messages?.at(-1)?.role==='assistant'?w.messages.at(-1).text.slice(0,2500):null,diagnostics:diag};
   }));
   if(r.turn.answer)r.turn.answer=r.turn.answer.replace(/sk-[A-Za-z0-9_-]+/g,'[REDACTED]').replace(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g,'[email]');
   r.verdict=r.turn.last_role==='assistant'&&!r.turn.status?'READ_ONLY_TURN_RETURNED':'ASSISTANT_TURN_FAILED';
  }else r.verdict='ASSISTANT_SESSION_FAILED';
 }catch(e){r.verdict='CHECK_FAILED';r.error=/timeout/i.test(e.message)?'timeout':e.code||e.name;}
 finally{if(c){await c.close();r.own_tabs_closed=true;}fs.writeFileSync(__dirname+'/windows-assistant.json',JSON.stringify(r,null,2)+'\n');}
 process.stdout.write(JSON.stringify(r)+'\n');
})();
