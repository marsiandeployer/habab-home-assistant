'use strict';

async function clearFocusedTextareaWithKeyboard(keyboard) {
  await keyboard.down('Control');
  try {
    await keyboard.press('KeyA');
  } finally {
    await keyboard.up('Control');
  }
  await keyboard.press('Backspace');
}

module.exports = {clearFocusedTextareaWithKeyboard};
