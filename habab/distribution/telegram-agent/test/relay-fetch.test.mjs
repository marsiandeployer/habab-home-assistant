import test from 'node:test';
import assert from 'node:assert/strict';
import {createTelegramFetch} from '../relay-fetch.mjs';
import {TelegramTransport} from '../vendor/noxon-telegram/src/index.mjs';
const token = () => 'synthetic-bot-token-for-tests';
const waitAbort = (signal) => new Promise((resolve,reject) => {
  const fail = () => reject(new Error('aborted'));
  if(signal.aborted) fail(); else signal.addEventListener('abort', fail, {once:true});
});
const response = (result) => ({ok:true,status:200,json:async()=>({ok:true,result})});

test('direct transport preserves URL, options and result', async()=>{
 const options={method:'POST'}, expected=response({}), calls=[];
 const fetch=createTelegramFetch({token,fetchImpl:async(...args)=>{calls.push(args);return expected;}});
 assert.equal(await fetch('https://api.telegram.org/botsynthetic-bot-token-for-tests/getMe',options),expected);
 assert.deepEqual(calls,[['https://api.telegram.org/botsynthetic-bot-token-for-tests/getMe',options]]);
});

test('relay rejects unsafe bases and methods before sending credentials',async()=>{
 for(const relay of ['http://relay.test','https://user@relay.test','https://relay.test/path','https://relay.test?key=x'])
  assert.throws(()=>createTelegramFetch({relay,token}),/invalid_telegram_relay/);
 let called=false;
 const fetch=createTelegramFetch({relay:'https://relay.test',token,fetchImpl:()=>{called=true;}});
 await assert.rejects(fetch('https://api.telegram.org/botsynthetic-bot-token-for-tests/deleteWebhook'),/invalid_telegram_operation/);
 await assert.rejects(fetch('https://other.test/getMe'),/invalid_telegram_operation/);
 assert.equal(called,false);
});

test('relay URL omits token; credential is only in the authorization header',async()=>{
 let seen;
 const fetch=createTelegramFetch({relay:'https://relay.test/',token,fetchImpl:async(url,options)=>{seen={url,options};return response({username:'fixture'});}});
 await fetch('https://api.telegram.org/botsynthetic-bot-token-for-tests/getMe',{method:'POST',body:'{}'});
 assert.equal(seen.url,'https://relay.test/telegram/getMe');
 assert.equal(seen.options.headers.authorization,'Bearer synthetic-bot-token-for-tests');
 assert.equal(seen.options.body,'{}');
});

test('deadline includes a stalled response body; caller abort cancels poll',async()=>{
 const fetch=createTelegramFetch({relay:'https://relay.test',token,timeoutMs:20,pollTimeoutMs:200,
  fetchImpl:async(url,{signal})=>({ok:true,status:200,json:()=>waitAbort(signal)})});
 await assert.rejects(fetch('https://api.telegram.org/botsynthetic-bot-token-for-tests/sendMessage'),/aborted/);
 const controller=new AbortController();
 const poll=fetch('https://api.telegram.org/botsynthetic-bot-token-for-tests/getUpdates',{signal:controller.signal});
 controller.abort();await assert.rejects(poll,/aborted/);
});

test('timed out send retries settle and the serialized queue accepts the next send',async()=>{
 const events=[];let sends=0;
 const fetch=createTelegramFetch({relay:'https://relay.test',token,timeoutMs:20,
  fetchImpl:async(url,{signal})=>{
   if(url.endsWith('/getMe'))return response({username:'fixture'});
   if(url.endsWith('/getUpdates'))return waitAbort(signal);
   if(url.endsWith('/sendMessage')&&++sends<=2)return waitAbort(signal);
   return response({message_id:1});
  }});
 const transport=new TelegramTransport({fetchImpl:fetch,emit:event=>events.push(event)});
 try{
  await transport.configure(token());
  await transport.send(10,'first','first');
  await transport.send(10,'second','second');
  assert.equal(sends,3);
  assert.ok(events.some(e=>e.type==='error'&&e.request_id==='first'));
  assert.ok(events.some(e=>e.type==='sent'&&e.request_id==='second'));
 }finally{await transport.shutdown();}
});
