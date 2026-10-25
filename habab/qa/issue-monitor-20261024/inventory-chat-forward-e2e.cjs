'use strict';

// Read-only owner-test actual rendered chat QA: inventory mode or bounded webtools mode.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const puppeteer = require('../../node_modules/puppeteer-core');
const {withOwnedChromium} = require('/root/tools/chrome-daemon/owned-browser.cjs');
const {NativeHaServiceObserver, selectedAssistantProvider, isReadyToSubmit} = require('./service-call-e2e.cjs');

const ORIGIN = 'https://habab-demo.wpmix.net';
const AUTH = '/root/.local/share/habab-owner-test/qa-current-auth.json';
const IDENTITY = '/root/.local/share/habab-owner-test/frontend8-hotfix-runtime-identity.json';
const WEBTOOLS_MODE = process.argv.includes('--webtools');
const FRESH_HOME_MODE = process.argv.includes('--fresh-home');
if (WEBTOOLS_MODE && FRESH_HOME_MODE) throw new Error('choose_one_runner_mode');
const MODE = WEBTOOLS_MODE ? 'webtools' : FRESH_HOME_MODE ? 'fresh-home' : 'inventory';
const PRIVATE_DIR = MODE === 'webtools'
  ? '/root/.local/share/habab-owner-test/webtools-chat-e2e'
  : MODE === 'fresh-home'
    ? '/root/.local/share/habab-owner-test/fresh-home-chat-e2e'
    : '/root/.local/share/habab-owner-test/inventory-chat-forward-e2e';
const ATTEMPT = MODE === 'inventory' ? 'attempt3' : 'attempt1';
const PUBLIC = path.join(__dirname, MODE === 'inventory'
  ? `inventory-chat-forward-e2e-${ATTEMPT}.json` : `${MODE}-chat-e2e-${ATTEMPT}.json`);
const PRIVATE = path.join(PRIVATE_DIR, `${ATTEMPT}.json`);
const RECOVERED = path.join('/root/.local/share/habab-owner-test/inventory-chat-forward-e2e', 'attempt1-recovered-session.json');
const EXPECTED_IMAGE = 'sha256:fc6062a20172affd5a7218d0586e29580ccbb0e876596ae10ba13360059bc928';
const EXPECTED_ACTIONS = '4769b330f11477ce49ab8ce262cd138ac4f4e459d82348cc6db961d72aad59f9';
const EXPECTED_AGENT = '3684a3f235dd06be7c038c18dc695182695b734c80365a4f9cd0fbde49d82d37';
const PREVIOUS_AGENT = 'a565fe8ca2461f7eed52f38c82a0198fc4f1ea01aaded7959fde6affd0d4916e';
const INVENTORY_PROMPTS = [
  'Проверь в Home Assistant, какие устройства сейчас подключены и сколько их. Коротко перечисли их типы. Ничего не подключай, не меняй и не запускай.',
  'Проверь, есть ли у меня отдельное физическое устройство для погоды или температуры либо только интеграция Met.no. Сверь реестр устройств и сущностей; ничего не запускай.',
  'Какая модель моего пылесоса указана в Home Assistant и сколько сущностей к нему относится? Ничего не запускай.',
  'Сколько всего устройств в реестре Home Assistant и сколько из них подтверждаются как физические по производителю и модели? Не считай сущности или сервисы устройствами; ничего не меняй.',
  'Коротко объясни, что из подключённого у меня является реальным устройством, а что только интеграцией или сервисом. Ничего не меняй и не запускай.',
];
const WEBTOOLS_PROMPTS = [
  'Используй инструмент webfetch, чтобы открыть именно https://www.home-assistant.io/integrations/xiaomi_miio/. Коротко скажи, что описывает эта страница и какие требования или ограничения она указывает. Не используй shell/curl, не меняй настройки и не запускай устройства.',
  'С помощью websearch проверь по официальным источникам Home Assistant, какая актуальная стабильная версия Home Assistant Core на сегодня. Укажи номер версии, дату публикации и ссылку на официальный источник. Не отвечай по памяти и ничего не меняй.',
];
const FRESH_HOME_PROMPTS = [
  'Пылесос сейчас убирает или находится на базе? Проверь текущее состояние. Ничего не меняй и не запускай.',
  'В прошлой переписке ты говорил, что пылесос убирает. Это всё ещё так? Только проверь, без команд устройству.',
];
const PROMPTS = WEBTOOLS_MODE ? WEBTOOLS_PROMPTS : FRESH_HOME_MODE ? FRESH_HOME_PROMPTS : INVENTORY_PROMPTS;

function writeJson(file, data, mode, flag = 'wx') {
  fs.writeFileSync(file, `${JSON.stringify(data, null, 2)}\n`, {mode, flag});
  fs.chmodSync(file, mode);
}
function safeError(error) {
  return error instanceof Error && /^[A-Za-z0-9_]{1,50}$/.test(error.name) ? error.name : 'Error';
}
function summarizeRegistry(devices, entities, entries) {
  return {
    device_count: devices.length,
    device_domains: [...new Set(entities.map((entity) => entity.entity_id?.split('.')[0]).filter(Boolean))].sort(),
    entity_count: entities.length,
    entity_domains: entities.reduce((out, entity) => {
      const domain = entity.entity_id?.split('.')[0];
      if (domain) out[domain] = (out[domain] || 0) + 1;
      return out;
    }, {}),
    config_entry_domains: entries.reduce((out, entry) => {
      if (entry.domain) out[entry.domain] = (out[entry.domain] || 0) + 1;
      return out;
    }, {}),
    devices,
    entities,
    config_entries: entries,
  };
}
function hasCompletedTurn(messages, prompt, callback) {
  const latestUserIndex = Array.isArray(messages)
    ? messages.findLastIndex((message) => message?.role === 'user') : -1;
  const latestUser = latestUserIndex >= 0 ? messages[latestUserIndex] : null;
  const assistantPresent = latestUserIndex >= 0 && messages.slice(latestUserIndex + 1)
    .some((message) => message?.role === 'assistant' && typeof message.text === 'string' && message.text.trim());
  return callback?.kind === 'completed' && callback.accepted === true && callback.latest_user_match === true
    && callback.assistant_present === true && callback.final_busy === false
    && latestUser?.text === prompt && assistantPresent;
}
async function readSelectedAssistantProvider(page) {
  return selectedAssistantProvider(await page.evaluate(() =>
    document.querySelector('habab-setup-chat')?.hass?.callWS({type:'habab_home/providers/list'})));
}
async function readBackendDiagnostics(observer, sessionId) {
  try {
    const result=await observer.command({type:'habab_home/agent/diagnostics',session_id:sessionId},{timeoutMs:7000});
    return {active:typeof result?.active==='boolean'?result.active:null,error:result?.error||null};
  } catch { return {active:null,error:'diagnostics_unavailable'}; }
}

async function main() {
  fs.mkdirSync(PRIVATE_DIR, {recursive: true, mode: 0o700});
  fs.chmodSync(PRIVATE_DIR, 0o700);
  if (fs.existsSync(PUBLIC) || fs.existsSync(PRIVATE)) throw new Error('receipt_already_exists');
  const identity = JSON.parse(fs.readFileSync(IDENTITY, 'utf8'));
  const authStat = fs.statSync(AUTH);
  if ((authStat.mode & 0o077) !== 0 || Date.now() / 1000 - authStat.mtimeMs / 1000 > 1200) {
    throw new Error('auth_not_fresh_or_private');
  }
  const auth = JSON.parse(fs.readFileSync(AUTH, 'utf8'));
  if (!auth.access_token || new URL(auth.client_id).origin !== ORIGIN) throw new Error('auth_target_mismatch');
  let recovered=null;
  if (MODE === 'inventory') {
    recovered = JSON.parse(fs.readFileSync(RECOVERED, 'utf8'));
    if ((fs.statSync(RECOVERED).mode & 0o077) !== 0) throw new Error('recovered_trace_permissions_invalid');
    const previousTrace = JSON.parse(fs.readFileSync(path.join(PRIVATE_DIR,'attempt1.json'),'utf8'));
    const previousAgent = previousTrace.runtime_identity?.forward_hotfix_files?.['setup_agent.py'];
    if (previousAgent !== PREVIOUS_AGENT) throw new Error('previous_build_identity_unavailable');
  }
  if (identity.runtime !== 'habab-owner-test' || identity.image !== EXPECTED_IMAGE
      || identity.forward_hotfix_files?.setup_actions_py !== EXPECTED_ACTIONS) {
    // Current identity uses filenames as keys; validate the authoritative values below too.
    const hotfixes = identity.forward_hotfix_files || {};
    if (identity.runtime !== 'habab-owner-test' || identity.image !== EXPECTED_IMAGE
      || !Object.values(hotfixes).includes(EXPECTED_ACTIONS)
      || !Object.values(hotfixes).includes(EXPECTED_AGENT)) throw new Error('runtime_identity_mismatch');
  }
  const privateTrace = {started_at: new Date().toISOString(), runtime_identity: identity,
    stage: 'preflight', turns: [], browser_events: [], service_event_count: null};
  let observer;
  let verdict = 'FAIL';
  let stage = 'preflight';
  const report = {checked_at: new Date().toISOString(), target: 'owner-test', verdict: 'IN_PROGRESS',
    runtime_image: identity.image, runtime_source: identity.source,
    current_setup_agent_sha256: EXPECTED_AGENT,
    ...(WEBTOOLS_MODE ? {purpose:'actual_webfetch_and_websearch_tool_acceptance',build_boundary_between_prior_turn_and_remaining_turns:false}
      : FRESH_HOME_MODE ? {purpose:'fresh_home_snapshot_read_only_acceptance',preserve_existing_session_history:true}
      : {previous_turn_setup_agent_sha256:PREVIOUS_AGENT,build_boundary_between_prior_turn_and_remaining_turns:true}),
    result: 'pending_independent_semantic_review', prompts_completed: 0, prompts_expected: PROMPTS.length,
    owner_admin_verified: false, existing_session_reused: false, selected_provider_verified: false,
    native_service_observer_continuous: null, service_calls_during_run: null,
    registry_unchanged: null, backend_idle_at_end: null,
    limitations: [WEBTOOLS_MODE ? 'Raw mapped OpenCode tool parts must confirm webfetch and websearch; model text alone is not tool evidence.'
      : FRESH_HOME_MODE ? 'Raw mapped turns must show a fresh home snapshot in synthetic runtime-status parts; answers alone are not snapshot evidence.'
      : 'Assistant answer semantics require independent comparison with the private HA registry and tool trace.']};
  try {
    // Browser resolves the actual owner id before the independent native observer authenticates.
    await withOwnedChromium(puppeteer, {taskTimeoutMs: 900000,
      args: ['--disable-dev-shm-usage'], defaultViewport: {width: 1280, height: 800}}, async ({page}) => {
      const signals = new Map();
      await page.exposeFunction('__hababQaTurnSignal', (event) => {
        privateTrace.browser_events.push({kind:event.kind,case_id:event.case_id,accepted:event.accepted,
          latest_user_match:event.latest_user_match,assistant_present:event.assistant_present,
          roles:event.roles,final_busy:event.final_busy,error_class:event.error||null});
        if (event.kind === 'completed' || event.kind === 'failed') signals.get(event.case_id)?.(event);
      });
      page.on('pageerror', (error) => privateTrace.browser_events.push({kind:'pageerror', class:safeError(error)}));
      page.on('requestfailed', (request) => {
        try { privateTrace.browser_events.push({kind:'requestfailed', pathname:new URL(request.url()).pathname}); } catch {}
      });
      page.on('response', (response) => { if (response.status() >= 400) {
        try { privateTrace.browser_events.push({kind:'http_error', status:response.status(), pathname:new URL(response.url()).pathname}); } catch {}
      }});
      await page.evaluateOnNewDocument((tokens, origin) => {
        localStorage.setItem('hassTokens', JSON.stringify({...tokens, hassUrl: origin,
          clientId: tokens.client_id, expires: Date.now() + (tokens.expires_in || 3600) * 1000}));
        localStorage.setItem('selectedLanguage', JSON.stringify('ru'));
      }, {access_token:auth.access_token, refresh_token:auth.refresh_token, token_type:auth.token_type,
        expires_in:auth.expires_in, client_id:auth.client_id}, ORIGIN);
      stage = 'owner_test_route';
      await page.goto(`${ORIGIN}/owner-test`, {waitUntil:'networkidle2', timeout:60000});
      stage = 'overview_route';
      await page.goto(`${ORIGIN}/home/overview`, {waitUntil:'networkidle2', timeout:60000});
      await page.waitForFunction(() => {
        const chat=document.querySelector('habab-setup-chat');
        return !!chat?.hass?.user?.is_admin && !!chat.hass.callWS && !!window.hababAssistant?.available
          && !!chat.sessionId && !chat.busy && !chat.running;
      }, {timeout:90000});
      const baseline = await page.evaluate(async () => {
        const chat=document.querySelector('habab-setup-chat');
        const providers=await chat.hass.callWS({type:'habab_home/providers/list'});
        const [devices, entities, entries]=await Promise.all([
          chat.hass.callWS({type:'config/device_registry/list'}),
          chat.hass.callWS({type:'config/entity_registry/list'}),
          chat.hass.callWS({type:'config_entries/get'}),
        ]);
        return {admin:chat.hass.user?.is_admin===true, owner_id:chat.hass.user?.id||null,
          session_id:chat.sessionId||null, chat_id:chat.__hababQaInstance||null,
          busy:!!chat.busy||!!chat.running, messages:chat.messages.map(({role,text})=>({role,text})),
          dialog_open:!!chat.shadowRoot.querySelector('dialog')?.open,
          draft:chat.shadowRoot.querySelector('textarea')?.value||'', providers,
          registry:{devices,entities,entries}};
      });
      if (!baseline.admin || !baseline.owner_id || !baseline.session_id || baseline.busy || baseline.draft) throw new Error('owner_session_not_idle');
      const chosen = await readSelectedAssistantProvider(page);
      // Authenticate the independent observer against the actual browser owner.
      observer = new NativeHaServiceObserver(auth.access_token);
      await observer.authenticateOwner(baseline.owner_id);
      await observer.subscribeCallService();
      if (!observer.snapshot().connected) throw new Error('native_observer_not_connected');
      const diagStart = await readBackendDiagnostics(observer, baseline.session_id);
      if (diagStart.active !== false) throw new Error('backend_not_idle_before_turns');
      if (baseline.dialog_open) {
        // Existing open panel is fine; otherwise open using the visible launcher.
      } else {
        const launcher = await page.evaluate(() => {
          const rect=document.querySelector('habab-setup-chat')?.shadowRoot?.querySelector('.launcher')?.getBoundingClientRect();
          return rect ? {x:rect.left+rect.width/2,y:rect.top+rect.height/2} : null;
        });
        if (!launcher) throw new Error('chat_launcher_missing');
        await page.mouse.click(launcher.x,launcher.y);
        await page.waitForFunction(() => document.querySelector('habab-setup-chat')?.shadowRoot?.querySelector('dialog')?.open===true,{timeout:5000});
      }
      const ready = await page.evaluate(() => {
        const chat=document.querySelector('habab-setup-chat');
        const textarea=chat.shadowRoot.querySelector('textarea');
        window.__hababQaInventoryBaselineChat=chat;
        return {owner_id:chat.hass.user?.id,admin:chat.hass.user?.is_admin===true,session_id:chat.sessionId,
          idle:!chat.busy&&!chat.running, dialog:chat.shadowRoot.querySelector('dialog')?.open===true,
          draft:textarea?.value||'', messages:chat.messages.map(({role,text})=>({role,text}))};
      });
      if (ready.owner_id!==baseline.owner_id || !ready.admin || ready.session_id!==baseline.session_id || !ready.idle || !ready.dialog || ready.draft) throw new Error('chat_ready_context_mismatch');
      const baselineRegistry=summarizeRegistry(baseline.registry.devices,baseline.registry.entities,baseline.registry.entries);
      let recoveredUserIndex=-1;
      if (MODE === 'inventory') {
        if (baseline.session_id !== recovered.session_id) throw new Error('recovered_session_mismatch');
        recoveredUserIndex=recovered.messages.findLastIndex((message)=>message?.role==='user');
        if (recovered.messages[recoveredUserIndex]?.text!==PROMPTS[0]
            || !recovered.messages.slice(recoveredUserIndex+1).some((message)=>message?.role==='assistant'&&message.text?.trim())) {
          throw new Error('recovered_turn_not_exact_or_incomplete');
        }
      }
      privateTrace.baseline={owner_id:baseline.owner_id,session_id:baseline.session_id,provider:chosen,
        history_count:baseline.messages.length, registry:baselineRegistry,
        observer:observer.snapshot(), backend_active:diagStart.active,
        ...(WEBTOOLS_MODE ? {purpose:'webtools',setup_agent_sha256:EXPECTED_AGENT}
          : FRESH_HOME_MODE ? {purpose:'fresh_home_snapshot',setup_agent_sha256:EXPECTED_AGENT,
            history_preserved_without_reset:true,existing_history_count:baseline.messages.length}
          : {build_boundary:{previous_setup_agent_sha256:PREVIOUS_AGENT,current_setup_agent_sha256:EXPECTED_AGENT,
            previous_prompt_semantic_status:'fail_no_same_turn_device_tool_invocation'}})};
      report.owner_admin_verified=true; report.existing_session_reused=true; report.selected_provider_verified=true;
      report.native_service_observer_continuous=true;

      let eventBoundary=observer.events.length;
      let startPromptIndex=0;
      if (MODE === 'inventory') {
        const recoveredCallback={kind:'completed',accepted:true,latest_user_match:true,assistant_present:true,final_busy:false,
          recovered_from_read_only_native_session:true};
        const recoveredTurn={index:1,case_id:'recovered-attempt1',prompt:PROMPTS[0],
          callback:recoveredCallback,turn:{messages:recovered.messages,latest_user:PROMPTS[0],
            roles_after_latest_user:recovered.messages.slice(recoveredUserIndex+1).map((message)=>message.role),
            latest_user_match:true,assistant_present:true,final_busy:false},
          full_user_prompts:recovered.messages.filter((message)=>message.role==='user').map((message)=>message.text),
          backend_active:false,service_event_count:0,recovered_read_only:true,
          setup_agent_sha256:PREVIOUS_AGENT,semantic_status:'fail_no_same_turn_device_tool_invocation'};
        if (!hasCompletedTurn(recovered.messages,PROMPTS[0],recoveredCallback)) throw new Error('recovered_turn_completion_assertion_failed');
        privateTrace.turns.push(recoveredTurn);
        report.prompts_completed=1;
        writeJson(PRIVATE,privateTrace,0o600,'w');
        startPromptIndex=1;
      }
      for (let i=startPromptIndex;i<PROMPTS.length;i++) {
        stage=`turn_${i+1}_preflight`;
        if (!observer.snapshot().connected || !observer.snapshot().owner_matches) throw new Error('observer_disconnected_or_owner_mismatch');
        if (observer.events.length!==eventBoundary) throw new Error('unexpected_service_call_between_turns');
        const d=await readBackendDiagnostics(observer,baseline.session_id);
        if (d.active!==false) throw new Error('backend_not_idle_before_turn');
        const observerBeforeSubmit=observer.snapshot();
        const readiness=await page.evaluate(({ownerId,sessionId,diagnosticsActive,connection})=>{
          const chat=document.querySelector('habab-setup-chat');
          const textarea=chat?.shadowRoot?.querySelector('textarea');const send=chat?.shadowRoot?.querySelector('.send');
          const dialog=chat?.shadowRoot?.querySelector('dialog');
          const inert=(element)=>{
            for(let node=element;node;){
              if(node.inert||node.hasAttribute?.('inert')||node.getAttribute?.('aria-hidden')==='true')return true;
              const root=node.getRootNode?.();node=node.parentElement||(root instanceof ShadowRoot?root.host:null);
            }
            return false;
          };
          const visible=(element)=>{
            if(!element?.isConnected)return false;
            const r=element.getBoundingClientRect(),s=getComputedStyle(element);
            return r.width>0&&r.height>0&&r.bottom>0&&r.right>0&&r.top<innerHeight&&r.left<innerWidth
              &&s.display!=='none'&&s.visibility!=='hidden'&&s.pointerEvents!=='none';
          };
          const r=chat?.getBoundingClientRect();
          return {
            chat_present:!!chat,
            chat_visible:!!chat?.isConnected&&!chat.hidden&&!!r&&r.width>0&&r.height>0,
            chat_not_inert:!!chat&&!inert(chat),
            owner_admin:chat?.hass?.user?.is_admin===true,
            owner_matches:chat?.hass?.user?.id===ownerId&&chat?.isCurrentOwnerContext?.()===true,
            hook_available:!!window.hababAssistant?.available,
            session_ready:!!sessionId&&chat?.sessionId===sessionId,
            busy:!!chat?.busy,
            running:!!chat?.running,
            diagnostics_active:diagnosticsActive,
            connection_connected:connection.connected,
            connection_same:connection.same_connection,
            connection_disconnected_count:connection.disconnected_count,
            document_visible:document.visibilityState==='visible',
            dialog_open:dialog?.open===true,
            textarea_visible:visible(textarea),
            textarea_not_inert:!!textarea&&!inert(textarea),
            textarea_enabled:textarea?.disabled===false,
            textarea_empty:textarea?.value==='',
            send_visible:visible(send),
            send_not_inert:!!send&&!inert(send),
            send_enabled:send?.disabled===false,
          };
        },{ownerId:baseline.owner_id,sessionId:baseline.session_id,diagnosticsActive:d.active,connection:observerBeforeSubmit});
        privateTrace.preflights=privateTrace.preflights||[];
        const readinessRecord={index:i+1,phase:'before_input',readiness,ready:isReadyToSubmit(readiness)};
        privateTrace.preflights.push(readinessRecord);
        writeJson(PRIVATE,privateTrace,0o600,'w');
        if(!readinessRecord.ready){
          const failed=Object.entries(readiness).filter(([key,value])=>value===false).map(([key])=>key);
          readinessRecord.failed_fields=failed;writeJson(PRIVATE,privateTrace,0o600,'w');
          throw new Error(`turn_${i+1}_submit_readiness_failed`);
        }
        const caseId=`inventory-${i+1}-${crypto.randomBytes(5).toString('hex')}`;
        const installed=await page.evaluate(({prompt,caseId,ownerId,sessionId})=>{
          const chat=document.querySelector('habab-setup-chat');
          const textarea=chat?.shadowRoot?.querySelector('textarea'); const send=chat?.shadowRoot?.querySelector('.send');
          const dialog=chat?.shadowRoot?.querySelector('dialog');
          if (!chat||chat!==window.__hababQaInventoryBaselineChat||!chat.hass?.user?.is_admin||chat.hass.user.id!==ownerId
            ||chat.sessionId!==sessionId||chat.busy||chat.running||!dialog?.open||textarea?.value||send?.disabled) return false;
          if (!chat.__inventoryOriginalSend) {
            chat.__inventoryOriginalSend=chat.send;
            chat.send=async function(...args) {
              const expected=this.__inventoryProbe;
              const text=typeof args[0]==='string'?args[0].trim():this.shadowRoot.querySelector('textarea')?.value?.trim();
              if (!expected||text!==expected.prompt) return this.__inventoryOriginalSend.apply(this,args);
              const before=this.messages.length;
              window.__hababQaTurnSignal({kind:'started',case_id:expected.caseId,ts:Date.now()});
              let operation;
              try { operation=this.__inventoryOriginalSend.apply(this,args); }
              catch(error) { window.__hababQaTurnSignal({kind:'failed',case_id:expected.caseId,error:error?.name||'Error'}); throw error; }
              expected.accepted=this.busy===true&&this.messages.length===before+1&&this.messages.at(-1)?.role==='user'&&this.messages.at(-1)?.text===text;
              window.__hababQaTurnSignal({kind:'accepted',case_id:expected.caseId,accepted:expected.accepted});
              try { return await operation; }
              catch(error) { expected.error=error?.name||'Error'; throw error; }
              finally {
                const last=this.messages.findLastIndex((m)=>m?.role==='user');
                expected.completed=true; expected.latest_user_match=last>=0&&this.messages[last]?.text===expected.prompt;
                expected.assistant_present=last>=0&&this.messages.slice(last+1).some((m)=>m?.role==='assistant'&&m.text?.trim());
                expected.roles_after_latest_user=last>=0?this.messages.slice(last+1).map((m)=>m.role):[];
                expected.final_busy=!!this.busy||!!this.running;
                window.__hababQaTurnSignal({kind:'completed',case_id:expected.caseId,accepted:expected.accepted,
                  latest_user_match:expected.latest_user_match,assistant_present:expected.assistant_present,
                  roles:expected.roles_after_latest_user,final_busy:expected.final_busy,error:expected.error||null});
              }
            };
          }
          chat.__inventoryProbe={caseId,prompt};
          return true;
        },{prompt:PROMPTS[i],caseId,ownerId:baseline.owner_id,sessionId:baseline.session_id});
        if (!installed) throw new Error(`turn_${i+1}_chat_precondition_failed`);
        const input=await page.evaluate((prompt)=>{
          const chat=document.querySelector('habab-setup-chat');const t=chat.shadowRoot.querySelector('textarea');
          const r=t.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2,prompt};
        },PROMPTS[i]);
        await page.mouse.click(input.x,input.y);
        await page.keyboard.type(input.prompt,{delay:8});
        const target=await page.evaluate((prompt,ownerId)=>{
          const chat=document.querySelector('habab-setup-chat');const t=chat.shadowRoot.querySelector('textarea');
          const b=chat.shadowRoot.querySelector('.send');const r=b.getBoundingClientRect();const x=r.left+r.width/2,y=r.top+r.height/2;
          const hit=chat.shadowRoot.elementFromPoint(x,y);
          const style=getComputedStyle(b);
          return {x,y,text_ok:t.value===prompt,send_disabled:b.disabled,visible:r.width>0&&r.height>0,
            hit:hit===b||b.contains(hit),hit_tag:hit?.tagName||null,hit_class:String(hit?.className||''),
            rect:{left:r.left,top:r.top,width:r.width,height:r.height},
            display:style.display,visibility:style.visibility,pointer_events:style.pointerEvents,
            textarea_length:t.value.length,chat_busy:!!chat.busy,chat_running:!!chat.running,
            dialog_open:chat.shadowRoot.querySelector('dialog')?.open===true,
            owner_context:chat.hass.user?.is_admin===true&&chat.hass.user?.id===ownerId
              &&chat.isCurrentOwnerContext?.()===true,
            hook_available:!!window.hababAssistant?.available,
            document_visible:document.visibilityState==='visible',textarea_enabled:t.disabled===false,
            session:chat.sessionId};
        },PROMPTS[i],baseline.owner_id);
        privateTrace.preflights.push({index:i+1,phase:'after_input',target});
        writeJson(PRIVATE,privateTrace,0o600,'w');
        if (!target.text_ok||target.send_disabled||!target.visible||!target.hit||!target.dialog_open
            ||!target.owner_context||!target.hook_available||target.chat_busy||target.chat_running
            ||!target.document_visible||!target.textarea_enabled) throw new Error(`turn_${i+1}_post_input_readiness_failed`);
        const completion=new Promise((resolve,reject)=>{
          const timeout=setTimeout(()=>reject(new Error('turn_completion_timeout')),180000);
          signals.set(caseId,(event)=>{clearTimeout(timeout);signals.delete(caseId);
            if(event.kind==='completed')resolve(event);else reject(new Error('send_failed'));});
        });
        await page.mouse.click(target.x,target.y,{delay:30});
        const callback=await completion;
        const turn=await page.evaluate((caseId)=>{
          const c=document.querySelector('habab-setup-chat');const probe=c.__inventoryProbe;
          const latest=c.messages.findLastIndex((m)=>m?.role==='user');
          return {messages:c.messages.map(({role,text})=>({role,text})),probe:{...probe},
            roles_after_latest_user:latest>=0?c.messages.slice(latest+1).map((m)=>m.role):[],
            latest_user:latest>=0?c.messages[latest]?.text:null,dialog:c.shadowRoot.querySelector('dialog')?.open===true,
            owner_id:c.hass.user?.id,admin:c.hass.user?.is_admin===true,session_id:c.sessionId,
            busy:!!c.busy||!!c.running};
        },caseId);
        if (!turn.probe.completed||!turn.probe.accepted||!turn.probe.latest_user_match||!turn.probe.assistant_present
            ||turn.probe.final_busy||turn.latest_user!==PROMPTS[i]||turn.busy||turn.owner_id!==baseline.owner_id
            ||!turn.admin||turn.session_id!==baseline.session_id) throw new Error(`turn_${i+1}_acceptance_mismatch`);
        if (callback.final_busy||callback.latest_user_match!==true||callback.assistant_present!==true) throw new Error(`turn_${i+1}_callback_mismatch`);
        const diagnostics=await readBackendDiagnostics(observer,baseline.session_id);
        if (diagnostics.active!==false) throw new Error(`turn_${i+1}_backend_not_idle_after`);
        if (!observer.snapshot().connected||!observer.snapshot().owner_matches||observer.events.length!==eventBoundary) throw new Error(`turn_${i+1}_service_activity_or_observer_loss`);
        const fullUserPrompts=turn.messages.filter((m)=>m.role==='user').map((m)=>m.text);
        if (!hasCompletedTurn(turn.messages,PROMPTS[i],callback)) throw new Error(`turn_${i+1}_latest_user_completion_mismatch`);
        privateTrace.turns.push({index:i+1,case_id:caseId,prompt:PROMPTS[i],callback,turn,
          full_user_prompts:fullUserPrompts,backend_active:diagnostics.active,service_event_count:observer.events.length-eventBoundary});
        report.prompts_completed=i+1;
        stage=`turn_${i+1}_accepted`;
        writeJson(PRIVATE,privateTrace,0o600,'w');
        if (i<PROMPTS.length-1) {
          // No blind polling: diagnostics plus browser busy state are both already idle.
          const stillIdle=await page.evaluate(()=>{const c=document.querySelector('habab-setup-chat');return !c.busy&&!c.running&&!c.shadowRoot.querySelector('textarea')?.value});
          if (!stillIdle) throw new Error(`turn_${i+1}_not_ready_for_next`);
        }
        eventBoundary=observer.events.length;
      }
      stage='registry_readback';
      const registryAfterRaw=await page.evaluate(async()=>{
        const c=document.querySelector('habab-setup-chat').hass;
        const [devices,entities,entries]=await Promise.all([c.callWS({type:'config/device_registry/list'}),
          c.callWS({type:'config/entity_registry/list'}),c.callWS({type:'config_entries/get'})]);
        return {devices,entities,entries};
      });
      const after=summarizeRegistry(registryAfterRaw.devices,registryAfterRaw.entities,registryAfterRaw.entries);
      privateTrace.registry_after=after;
      const beforeHash=crypto.createHash('sha256').update(JSON.stringify(privateTrace.baseline.registry)).digest('hex');
      const afterHash=crypto.createHash('sha256').update(JSON.stringify(after)).digest('hex');
      if(beforeHash!==afterHash)throw new Error('registry_changed_during_readonly_run');
      if(observer.events.length!==eventBoundary||!observer.snapshot().connected)throw new Error('service_observer_final_check_failed');
      const finalDiag=await readBackendDiagnostics(observer,baseline.session_id);
      if(finalDiag.active!==false)throw new Error('backend_not_idle_at_end');
      privateTrace.ended_at=new Date().toISOString();privateTrace.stage='completed';
      report.registry_unchanged=true;report.backend_idle_at_end=true;report.service_calls_during_run=observer.events.length;
      if (WEBTOOLS_MODE) {
        report.result='browser_turns_complete_tool_parts_pending_review';
        report.verdict='PASS_PENDING_TOOL_REVIEW';
        verdict='PASS_PENDING_TOOL_REVIEW';
      } else if (FRESH_HOME_MODE) {
        report.result='browser_turns_complete_snapshot_parts_pending_review';
        report.verdict='PASS_PENDING_SNAPSHOT_REVIEW';
        verdict='PASS_PENDING_SNAPSHOT_REVIEW';
      } else {
        report.result='remaining_turns_complete_prior_turn_failed_semantic_check';
        report.verdict='PARTIAL_PRIOR_TURN_FAIL';
        verdict='PARTIAL_PRIOR_TURN_FAIL';
      }
    });
  } catch(error) {
    stage=stage||'failed';
    privateTrace.failure={stage,error_class:safeError(error),code:/^[a-z0-9_]{1,100}$/.test(error.message)?error.message:'qa_failed',at:new Date().toISOString()};
    privateTrace.ended_at=new Date().toISOString();
    report.verdict='FAIL';report.result='technical_failure';report.failure_code=privateTrace.failure.code;
  } finally {
    if(observer){report.native_service_observer_continuous=observer.snapshot().connected&&observer.snapshot().owner_matches;
      report.service_calls_during_run=observer.events.length;await observer.close();}
    report.checked_at=new Date().toISOString();
    try{writeJson(PRIVATE,privateTrace,0o600,'w')}catch{}
    writeJson(PUBLIC,report,0o644);
  }
  process.stdout.write(`${JSON.stringify({report:PUBLIC,private_trace:PRIVATE,verdict:report.verdict,stage})}\n`);
  if(verdict==='FAIL')process.exitCode=1;
}

// Puppeteer exposed callback used by the actual chat.send wrapper.
if(require.main===module){
  main().catch((error)=>{process.stderr.write(`${safeError(error)}\n`);process.exitCode=1});
}

module.exports={hasCompletedTurn};
