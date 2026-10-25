'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
  sameDeviceBinding,
  eventMatches,
  hasOwnedStart,
  latestTurnMessageEvidence,
  firstMatchingSignal,
  hasVerifiedSendCompletion,
  classifyTurnEvidence,
  classifyWaitFailure,
  canAttemptEmergencyStop,
  canAttemptEmergencyDock,
  emergencyRecoveryComplete,
  canStartVacuumFrom,
} = require('./vacuum-chat-e2e.cjs');
const {serviceEventProjection} = require('./service-call-e2e.cjs');

const identity = Object.freeze({entity_id: 'vacuum.qa_robot', entity_registry_id: 'entity-1',
  device_id: 'device-1', config_entry_id: 'entry-1', platform: 'xiaomi_miio', supported_features: 8216});
const ownerId = 'owner-test-id';
const turn = Object.freeze({case_id: 'case-1', event_offset: 0, session_id: 'session-1', owner_id: ownerId,
  document_marker: 'doc-1', chat_instance_id: 'chat-1'});
const callbacks = Object.freeze([
  {kind: 'send_started', case_id: 'case-1', document_marker: 'doc-1', chat_instance_id: 'chat-1',
    session_id: 'session-1', owner_id: ownerId},
  {kind: 'send_accepted', case_id: 'case-1', accepted: true, document_marker: 'doc-1',
    chat_instance_id: 'chat-1', session_id: 'session-1', owner_id: ownerId},
]);
const startEvent = Object.freeze({event_type: 'call_service', domain: 'vacuum', service: 'start',
  entity_ids: ['vacuum.qa_robot'], device_ids: [], area_ids: [], floor_ids: [], label_ids: [],
  target_fields: ['entity_id'], context_user_id: ownerId});

test('start preflight accepts an idle or docked robot and rejects every moving or unavailable state', () => {
  assert.equal(canStartVacuumFrom('idle'), true);
  assert.equal(canStartVacuumFrom('docked'), true);
  for (const state of ['cleaning', 'returning', 'paused', 'unavailable', 'unknown', 'error', null]) {
    assert.equal(canStartVacuumFrom(state), false, `do not start from ${state}`);
  }
});

test('vacuum matcher accepts Core service_data entity target with empty target_fields', () => {
  const projected = serviceEventProjection({event_type: 'call_service', context: {user_id: ownerId}, data: {
    domain: 'vacuum', service: 'start', service_data: {entity_id: 'vacuum.qa_robot'}, target: {},
  }}, 0);
  assert.deepEqual(projected.entity_ids, ['vacuum.qa_robot']);
  assert.deepEqual(projected.target_fields, []);
  assert.equal(eventMatches(projected, 'vacuum.qa_robot', 'start', ownerId), true);
});

test('vacuum matcher still rejects missing, wrong, extra, and foreign-owner targets', () => {
  const project = (serviceData = {}, target = {}, contextOwner = ownerId) => serviceEventProjection({
    event_type: 'call_service', context: {user_id: contextOwner}, data: {
      domain: 'vacuum', service: 'start', service_data: serviceData, target,
    },
  }, 0);
  assert.equal(eventMatches(project(), 'vacuum.qa_robot', 'start', ownerId), false,
    'reject a projected service event with no entity target');
  assert.equal(eventMatches(project({entity_id: 'vacuum.other'}), 'vacuum.qa_robot', 'start', ownerId), false,
    'reject another entity target');
  assert.equal(eventMatches(project({entity_id: 'vacuum.qa_robot', device_id: 'device-2'}),
    'vacuum.qa_robot', 'start', ownerId), false, 'reject an additional device target');
  assert.equal(eventMatches(project({}, {entity_id: 'vacuum.qa_robot', area_id: 'area-2'}),
    'vacuum.qa_robot', 'start', ownerId), false, 'reject an additional area target');
  assert.equal(eventMatches(project({entity_id: 'vacuum.qa_robot'}, {}, 'other-owner'),
    'vacuum.qa_robot', 'start', ownerId), false, 'reject another owner context');
});

test('a changed registry binding blocks emergency service actions', () => {
  assert.equal(sameDeviceBinding(identity, identity), true);
  const rebound = {...identity, device_id: 'different-device'};
  const bindingMatches = sameDeviceBinding(identity, rebound);
  assert.equal(bindingMatches, false);
  assert.equal(canAttemptEmergencyStop({ownedStart: true, bindingMatches, mappedSession: true,
    ownerAdmin: true, state: 'cleaning'}), false);
  assert.equal(canAttemptEmergencyStop({ownedStart: true, bindingMatches: true, mappedSession: false,
    ownerAdmin: true, state: 'cleaning'}), false);
  assert.equal(canAttemptEmergencyStop({ownedStart: true, bindingMatches: true, mappedSession: true,
    ownerAdmin: false, state: 'cleaning'}), false);
});

test('movement without this run accepted prompt and exact start event blocks recovery', () => {
  assert.equal(hasOwnedStart([startEvent], identity.entity_id, ownerId, turn, []), false);
  assert.equal(canAttemptEmergencyStop({ownedStart: false, bindingMatches: true, mappedSession: true,
    ownerAdmin: true, state: 'cleaning'}), false);
});

test('start ownership requires event after the accepted chat turn for the exact target and owner', () => {
  assert.equal(hasOwnedStart([startEvent], identity.entity_id, ownerId, turn, callbacks), true);
  assert.equal(hasOwnedStart([{...startEvent, context_user_id: 'other-owner'}], identity.entity_id, ownerId, turn, callbacks), false);
  assert.equal(hasOwnedStart([{...startEvent, entity_ids: ['vacuum.other']}], identity.entity_id, ownerId, turn, callbacks), false);
  assert.equal(hasOwnedStart([startEvent], identity.entity_id, ownerId, turn, callbacks.slice(1)), false);
});

test('failed or unverified stop never permits emergency docking', () => {
  for (const stopEventVerified of [false, undefined]) {
    assert.equal(canAttemptEmergencyDock({stopEventVerified, bindingMatches: true, mappedSession: true,
      ownerAdmin: true, state: 'idle'}), false);
  }
  assert.equal(canAttemptEmergencyDock({stopEventVerified: true, bindingMatches: true, mappedSession: true,
    ownerAdmin: true, state: 'cleaning'}), false);
});

test('unverified dock call remains incomplete even if the readback later says docked', () => {
  const stopCall = {ws_result: true, event_verified: true};
  const stoppedState = {state: 'idle'};
  const dockedState = {state: 'docked'};
  assert.equal(emergencyRecoveryComplete({stopCall, stoppedState,
    dockCall: {ws_result: false, event_verified: false}, dockedState}), false);
  assert.equal(emergencyRecoveryComplete({stopCall, stoppedState,
    dockCall: {ws_result: true, event_verified: false}, dockedState}), false);
  assert.equal(emergencyRecoveryComplete({stopCall, stoppedState,
    dockCall: {ws_result: true, event_verified: true}, dockedState}), true);
});

test('current turn correlation returns only assistant presence and length', () => {
  const prompt = 'Run this vacuum action through chat';
  const capped = Array.from({length: 57}, (_, index) => ({role: index % 2 ? 'assistant' : 'user', text: `old ${index}`}));
  capped.push({role: 'user', text: prompt},
    {role: 'assistant', text: 'I am checking the request.'},
    {role: 'assistant', text: 'The requested action is complete.'});
  assert.equal(capped.length, 60);
  const cappedEvidence = latestTurnMessageEvidence(capped, prompt);
  assert.deepEqual(cappedEvidence, {latest_user_match: true, assistant_present: true,
    assistant_length: 'The requested action is complete.'.length});
  assert.equal(Object.hasOwn(cappedEvidence, 'assistant_reply'), false);

  const replaced = [
    {role: 'assistant', text: 'Historical assistant response.'},
    {role: 'user', text: prompt},
    {role: 'assistant', text: 'Tool preface.'},
    {role: 'assistant', text: 'Current final answer.'},
  ];
  assert.deepEqual(latestTurnMessageEvidence(replaced, prompt), {
    latest_user_match: true, assistant_present: true, assistant_length: 'Current final answer.'.length,
  }, 'use the latest prompt and assistant messages after it after the history array is replaced');
  assert.equal(latestTurnMessageEvidence([
    {role: 'user', text: prompt}, {role: 'assistant', text: 'Old answer.'},
    {role: 'user', text: 'A newer prompt.'}, {role: 'assistant', text: 'New answer.'},
  ], prompt).latest_user_match, false, 'reject a newer different user message');
  assert.equal(latestTurnMessageEvidence([
    {role: 'assistant', text: 'Stale answer.'}, {role: 'user', text: prompt},
  ], prompt).assistant_present, false, 'do not use a stale assistant answer before the latest user');

  const baseline = {documentMarker: 'doc-1', chatInstanceId: 'chat-1'};
  const events = [
    {...callbacks[0]},
    {...callbacks[1]},
    {kind: 'send_completed', case_id: 'case-1', completed: true, accepted: true, final_busy: false,
      status_error: false, latest_user_match: true, assistant_present: true,
      document_marker: 'doc-1', chat_instance_id: 'chat-1', session_id: 'session-1', owner_id: ownerId,
      assistant_length: 'Готово'.length},
  ];
  assert.equal(hasVerifiedSendCompletion(events, 'case-1', baseline, replaced, prompt), true);
  assert.equal(hasVerifiedSendCompletion(events, 'case-1', baseline, [
    {role: 'user', text: prompt}, {role: 'assistant', text: 'Stale response'},
    {role: 'user', text: 'Newer input'}, {role: 'assistant', text: 'Newer response'},
  ], prompt), false, 'callback cannot override a superseding user message in the current UI history');
});

test('missing, failed, or replaced-document send callback cannot prove completion', () => {
  const baseline = {documentMarker: 'doc-1', chatInstanceId: 'chat-1'};
  const messages = [{role: 'user', text: 'Run this vacuum action through chat'},
    {role: 'assistant', text: 'Done.'}];
  const complete = [...callbacks, {kind: 'send_completed', case_id: 'case-1', completed: true,
    accepted: true, final_busy: false, status_error: false, latest_user_match: true, assistant_present: true,
    document_marker: 'doc-1', chat_instance_id: 'chat-1', session_id: 'session-1', owner_id: ownerId}];
  assert.equal(hasVerifiedSendCompletion(complete.slice(0, 2), 'case-1', baseline, messages,
    'Run this vacuum action through chat'), false);
  assert.equal(hasVerifiedSendCompletion(complete.map((event) => event.kind === 'send_accepted'
    ? {...event, accepted: false} : event), 'case-1', baseline, messages,
  'Run this vacuum action through chat'), false);
  assert.equal(hasVerifiedSendCompletion(complete.map((event) => event.kind === 'send_completed'
    ? {...event, document_marker: 'replacement-doc'} : event), 'case-1', baseline, messages,
  'Run this vacuum action through chat'), false);
});

test('turn evidence keeps lost telemetry unknown and wait failures retain only safe classifications', () => {
  const completed = classifyTurnEvidence([
    {kind: 'agent_run_probe_ready', case_id: 'case-1'},
    {kind: 'agent_run_started', case_id: 'case-1', prompt_match: true},
    {kind: 'agent_run_ack', case_id: 'case-1', prompt_match: true},
  ], 'case-1', 1);
  assert.deepEqual(completed, {submission: 'submitted', probe_ready_count: 1,
    agent_run_start_count: 1, agent_run_ack_count: 1, agent_run_error_count: 0,
    agent_run_error_code: null,
    acknowledged: true, terminal: 'completed', monitor_service_count: 1});

  const lostTelemetry = classifyTurnEvidence([
    {kind: 'agent_run_probe_ready', case_id: 'case-2'},
  ], 'case-2', 0);
  assert.equal(lostTelemetry.submission, 'unknown');
  assert.equal(lostTelemetry.acknowledged, null);
  assert.equal(lostTelemetry.terminal, 'unknown');

  const unknown = classifyTurnEvidence([
    {kind: 'agent_run_probe_ready', case_id: 'case-3'},
    {kind: 'agent_run_started', case_id: 'case-3', prompt_match: true},
  ], 'case-3', null);
  assert.equal(unknown.submission, 'unknown');
  assert.equal(unknown.acknowledged, null);
  assert.equal(unknown.terminal, 'unknown');
  assert.equal(unknown.monitor_service_count, null);

  const monitorOnly = classifyTurnEvidence([
    {kind: 'agent_run_probe_ready', case_id: 'case-4'},
  ], 'case-4', 1);
  assert.equal(monitorOnly.submission, 'unknown', 'an uncorrelated service event cannot prove this chat run');

  const failed = classifyTurnEvidence([
    {kind: 'agent_run_probe_ready', case_id: 'case-5'},
    {kind: 'agent_run_started', case_id: 'case-5', prompt_match: true},
    {kind: 'agent_run_failed', case_id: 'case-5', prompt_match: true, error_code: 'model_error'},
  ], 'case-5', 0);
  assert.equal(failed.submission, 'submitted');
  assert.equal(failed.acknowledged, false);
  assert.equal(failed.terminal, 'error');
  assert.equal(failed.agent_run_error_code, 'model_error');

  const timeout = Object.assign(new Error('private browser detail'), {name: 'TimeoutError'});
  const failure = classifyWaitFailure(timeout, 'chat_reply_timeout');
  assert.equal(failure.code, 'chat_reply_timeout');
  assert.equal(failure.name, 'TimeoutError');
  assert.equal(failure.message, 'chat_reply_timeout');
});

test('first matching callback at index zero is returned as a truthy signal object', () => {
  const first = {kind: 'send_started', case_id: 'case-1'};
  assert.equal(firstMatchingSignal([first, {kind: 'send_accepted', case_id: 'case-1'}],
    (event) => event.kind === 'send_started'), first);
  assert.equal(firstMatchingSignal([], () => true), null);
});
