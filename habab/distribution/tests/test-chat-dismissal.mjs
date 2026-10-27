import assert from "node:assert/strict";
import test from "node:test";
import {
  chatDetailDismissalId,
  chatDismissalStorageKey,
  readChatDismissals,
  writeChatDismissals,
} from "../src/habab_home/frontend/chat-dismissal.js";

test("dismissal identity is scoped to one owner, chat session, message, and block", () => {
  const key = chatDismissalStorageKey("owner-a", "session-a");
  assert.ok(key);
  assert.notEqual(key, chatDismissalStorageKey("owner-b", "session-a"));
  assert.notEqual(key, chatDismissalStorageKey("owner-a", "session-b"));

  const first = chatDetailDismissalId(4, 0, "reply", "QA card A");
  assert.notEqual(first, chatDetailDismissalId(4, 1, "reply", "QA card B"));
  assert.notEqual(first, chatDetailDismissalId(5, 0, "reply", "QA card A"));
  assert.notEqual(first, chatDetailDismissalId(4, 0, "different reply", "QA card A"));
});

test("dismissals persist and tolerate malformed or unavailable storage", () => {
  const data = new Map();
  const storage = {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => data.set(key, value),
  };
  const key = chatDismissalStorageKey("owner", "session");
  const ids = new Set(["one", "two"]);
  assert.equal(writeChatDismissals(storage, key, ids), true);
  assert.deepEqual(readChatDismissals(storage, key), ids);
  data.set(key, "not-json");
  assert.deepEqual(readChatDismissals(storage, key), new Set());
  assert.deepEqual(readChatDismissals(undefined, key), new Set());
  assert.equal(writeChatDismissals(undefined, key, ids), false);
});
