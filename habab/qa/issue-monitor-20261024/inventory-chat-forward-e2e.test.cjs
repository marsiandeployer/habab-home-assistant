'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {hasCompletedTurn} = require('./inventory-chat-forward-e2e.cjs');
const {isReadyToSubmit} = require('./service-call-e2e.cjs');

test('turn completion accepts exact latest prompt when history has capped older user rows', () => {
  const prompt = 'current read-only inventory question';
  const messages = [
    {role:'user', text:'older user turn'},
    {role:'assistant', text:'older answer'},
    {role:'user', text:prompt},
    {role:'assistant', text:'current answer'},
  ];
  const callback = {kind:'completed', accepted:true, latest_user_match:true,
    assistant_present:true, final_busy:false};
  assert.equal(hasCompletedTurn(messages, prompt, callback), true);
});

test('turn completion rejects missing assistant after exact latest prompt', () => {
  const prompt = 'current read-only inventory question';
  const messages = [{role:'user', text:prompt}];
  const callback = {kind:'completed', accepted:true, latest_user_match:true,
    assistant_present:false, final_busy:false};
  assert.equal(hasCompletedTurn(messages, prompt, callback), false);
});

test('submit readiness requires owner, live session, idle backend and actionable controls', () => {
  const ready = {
    chat_present:true, chat_visible:true, chat_not_inert:true,
    owner_admin:true, owner_matches:true, hook_available:true, session_ready:true,
    busy:false, running:false, diagnostics_active:false,
    connection_connected:true, connection_same:true, connection_disconnected_count:0,
    document_visible:true, dialog_open:true,
    textarea_visible:true, textarea_not_inert:true, textarea_enabled:true, textarea_empty:true,
    send_visible:true, send_not_inert:true, send_enabled:true,
  };
  assert.equal(isReadyToSubmit(ready), true);
  const blockingValues = {owner_matches:false,session_ready:false,hook_available:false,diagnostics_active:true,
    connection_connected:false,dialog_open:false,textarea_enabled:false,send_enabled:false};
  for (const [field,value] of Object.entries(blockingValues)) {
    assert.equal(isReadyToSubmit({...ready,[field]:value}), false, `${field} must block submit`);
  }
});
