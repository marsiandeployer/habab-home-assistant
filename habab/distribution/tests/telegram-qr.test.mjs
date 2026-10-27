import assert from "node:assert/strict";
import test from "node:test";
import {
  MAX_PAIRING_URL_LENGTH,
  copyTelegramPairingUrl,
  createTelegramPairingQr,
  normalizeTelegramPairingUrl,
  renderTelegramPairingQr,
  telegramQrText,
} from "../src/habab_home/frontend/telegram-qr.js";

test("owner and group Telegram URLs are locally validated before encoding", () => {
  const owner = "https://t.me/habab_home_bot?start=owner_nonce-123";
  const group = "https://t.me/habab_home_bot?startgroup=group_nonce-456";
  assert.equal(normalizeTelegramPairingUrl(owner, "owner"), owner);
  assert.equal(normalizeTelegramPairingUrl(group, "group"), group);
  assert.equal(normalizeTelegramPairingUrl(group, "owner"), "");
  assert.equal(normalizeTelegramPairingUrl(owner, "group"), "");
  assert.equal(normalizeTelegramPairingUrl("http://t.me/bot?start=nonce", "owner"), "");
  assert.equal(normalizeTelegramPairingUrl("https://example.com/bot?start=nonce", "owner"), "");
  assert.equal(normalizeTelegramPairingUrl("https://t.me/bot?start=nonce&redirect=https://example.com", "owner"), "");
  assert.equal(normalizeTelegramPairingUrl(`https://t.me/bot?start=${"x".repeat(MAX_PAIRING_URL_LENGTH)}`, "owner"), "");
});

test("local QR encoder produces a quiet-zone canvas for both pairing URL types", () => {
  for (const [kind, url] of [
    ["owner", "https://t.me/habab_home_bot?start=owner_nonce-123"],
    ["group", "https://t.me/habab_home_bot?startgroup=group_nonce-456"],
  ]) {
    const matrix = createTelegramPairingQr(url, kind);
    assert.ok(matrix.size >= 21);
    assert.equal(matrix.data.length, matrix.size * matrix.size);
    assert.ok(matrix.data.some((bit) => bit === 1));
    const calls = [];
    const canvas = {
      dataset: {},
      setAttribute(name, value) { this[name] = value; },
      getContext() {
        return {
          createImageData(width, height) {
            return { width, height, data: new Uint8ClampedArray(width * height * 4) };
          },
          putImageData(image, x, y) { calls.push({ image, x, y }); },
        };
      },
    };
    const modules = renderTelegramPairingQr(canvas, url, kind);
    assert.equal(modules, matrix.size);
    assert.equal(canvas.width, (matrix.size + 8) * 4);
    assert.equal(canvas.height, canvas.width);
    assert.equal(canvas.role, "img");
    assert.equal(calls.length, 1);
    assert.deepEqual([...calls[0].image.data.slice(0, 4)], [255, 255, 255, 255]);
  }
});

test("pairing QR rendering rejects untrusted URLs and exposes localized copy feedback", () => {
  assert.throws(() => createTelegramPairingQr("https://example.com/?start=secret", "owner"), /invalid_pairing_url/);
  assert.equal(telegramQrText("ru", "copyFailed"), "Не удалось скопировать ссылку. Откройте её вручную.");
  assert.equal(telegramQrText("en", "copyFailed"), "Could not copy the link. Open it manually.");
});

test("copy uses the browser clipboard and propagates failures for visible feedback", async () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  const writes = [];
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { clipboard: { writeText: async (value) => { writes.push(value); } } },
  });
  try {
    const url = "https://t.me/habab_home_bot?start=owner_nonce-123";
    await copyTelegramPairingUrl(url);
    assert.deepEqual(writes, [url]);
    globalThis.navigator.clipboard.writeText = async () => { throw new Error("permission_denied"); };
    await assert.rejects(copyTelegramPairingUrl(url), /permission_denied/);
  } finally {
    if (original) Object.defineProperty(globalThis, "navigator", original);
    else delete globalThis.navigator;
  }
});

test("copy falls back to a local selected textarea when Clipboard API is unavailable", async () => {
  const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  const copied = [];
  let textarea;
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: {} });
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      body: { append: (element) => { textarea = element; } },
      createElement: () => ({
        style: {}, setAttribute() {}, focus() {}, select() {}, setSelectionRange() {},
        remove() { this.removed = true; },
      }),
      execCommand(command) {
        assert.equal(command, "copy");
        copied.push(textarea.value);
        return true;
      },
    },
  });
  try {
    const url = "https://t.me/habab_home_bot?startgroup=group_nonce-456";
    await copyTelegramPairingUrl(url);
    assert.deepEqual(copied, [url]);
    assert.equal(textarea.removed, true);
  } finally {
    if (originalNavigator) Object.defineProperty(globalThis, "navigator", originalNavigator);
    else delete globalThis.navigator;
    if (originalDocument) Object.defineProperty(globalThis, "document", originalDocument);
    else delete globalThis.document;
  }
});
