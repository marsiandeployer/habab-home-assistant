'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {isOwnedHaSocket, classifyWebsocketFrame} = require('./diagnostics-download-live-readonly.cjs');

test('owned Home Assistant socket comparison maps page TLS to WSS and matches only API websocket', () => {
  assert.equal(isOwnedHaSocket('wss://ha.example.test/api/websocket', 'https://ha.example.test'), true);
  assert.equal(isOwnedHaSocket('ws://ha.example.test/api/websocket', 'http://ha.example.test'), true);
  assert.equal(isOwnedHaSocket('wss://ha.example.test/other', 'https://ha.example.test'), false);
  assert.equal(isOwnedHaSocket('wss://other.example.test/api/websocket', 'https://ha.example.test'), false);
  assert.equal(isOwnedHaSocket('ws://ha.example.test/api/websocket', 'https://ha.example.test'), false);
  assert.equal(isOwnedHaSocket('not a URL', 'https://ha.example.test'), false);
});

test('outgoing Home Assistant service calls expose only fixed domain/service metadata and reject non-log services', () => {
  assert.deepEqual(classifyWebsocketFrame(JSON.stringify({type: 'call_service', domain: 'system_log', service: 'write', service_data: {message: 'private'}})), {
    kind: 'system_log_write', service: 'system_log.write',
  });
  assert.deepEqual(classifyWebsocketFrame(JSON.stringify({type: 'call_service', domain: 'light', service: 'turn_on', service_data: {entity_id: 'light.private'}})), {
    kind: 'home_assistant_service', service: 'light.turn_on',
  });
  assert.deepEqual(classifyWebsocketFrame(JSON.stringify({type: 'call_service', domain: 'light', service: 'turn_on'})), {
    kind: 'home_assistant_service', service: 'light.turn_on',
  });
  assert.deepEqual(classifyWebsocketFrame(JSON.stringify({type: 'habab_home/agent/run', prompt: 'private'})), {
    kind: 'habab_write', operation: 'habab_home/agent/run',
  });
  assert.equal(classifyWebsocketFrame(JSON.stringify({type: 'habab_home/providers/list'})), undefined);
  assert.equal(classifyWebsocketFrame('not-json'), undefined);
});
