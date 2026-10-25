'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {CdpKeyboard} = require('/root/homeassistent/habab/node_modules/puppeteer-core/lib/cjs/puppeteer/cdp/Input.js');
const {clearFocusedTextareaWithKeyboard} = require('./frontend8-reload-guard-keyboard.cjs');

function createKeyboard(sendImpl = async () => undefined) {
  const commands = [];
  const keyboard = new CdpKeyboard({
    async send(method, params) {
      commands.push({method, ...params});
      return sendImpl(method, params);
    },
  });
  return {keyboard, commands};
}

test('installed Puppeteer CdpKeyboard rejects chord strings', async () => {
  const {keyboard, commands} = createKeyboard();
  await assert.rejects(keyboard.press('Control+A'), /Unknown key/);
  assert.equal(commands.length, 0);
});

test('select-all and delete uses real key names and releases Control', async () => {
  const {keyboard, commands} = createKeyboard();
  await clearFocusedTextareaWithKeyboard(keyboard);
  const keyEvents = commands.filter(command => command.method === 'Input.dispatchKeyEvent');
  assert.deepEqual(keyEvents.map(({type, code, modifiers}) => ({type, code, modifiers})), [
    {type: 'rawKeyDown', code: 'ControlLeft', modifiers: 2},
    {type: 'rawKeyDown', code: 'KeyA', modifiers: 2},
    {type: 'keyUp', code: 'KeyA', modifiers: 2},
    {type: 'keyUp', code: 'ControlLeft', modifiers: 0},
    {type: 'rawKeyDown', code: 'Backspace', modifiers: 0},
    {type: 'keyUp', code: 'Backspace', modifiers: 0},
  ]);
});

test('Control is released when dispatching KeyA fails', async () => {
  const {keyboard, commands} = createKeyboard((method, params) => {
    if (method === 'Input.dispatchKeyEvent' && params.code === 'KeyA' && params.type === 'rawKeyDown') {
      throw new Error('synthetic CDP send failure');
    }
  });
  await assert.rejects(clearFocusedTextareaWithKeyboard(keyboard), /synthetic CDP send failure/);
  assert.equal(commands.at(-1).code, 'ControlLeft');
  assert.equal(commands.at(-1).type, 'keyUp');
  assert.equal(commands.at(-1).modifiers, 0);
});
