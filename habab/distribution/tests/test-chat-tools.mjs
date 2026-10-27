import assert from "node:assert/strict";
import test from "node:test";
import {createToolCalls} from "../src/habab_home/frontend/chat-tools.js";
import {strings} from "../src/habab_home/frontend/strings.js";

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.open = false; this.textContent = ""; }
  append(...children) { this.children.push(...children); }
  set innerHTML(_value) { throw new Error("Tool output must never use innerHTML"); }
}
const document = {createElement: (tag) => new Element(tag)};
const find = (node, tag) => typeof node === "string" ? []
  : [...(node.tagName === tag ? [node] : []), ...node.children.flatMap((child) => find(child, tag))];

test("actual tool result is collapsed and rendered as literal text", () => {
  const value = '<img src=x onerror="alert(1)">';
  const group = createToolCalls(document, [{name: "bash", status: "completed", input: "habab-ha states", output: value}], strings.ru);
  assert.equal(group.open, false);
  assert.equal(group.children[0].textContent, "Инструменты OpenCode: 1");
  assert.equal(find(group, "details")[1].open, false);
  assert.deepEqual(find(group, "pre").map((node) => node.textContent), ["habab-ha states", value]);
});

test("failed calls show recorded errors and truncation links without inventing output", () => {
  const group = createToolCalls(document, [{name: "webfetch", status: "error", error: "request failed", truncated: true}], strings.en);
  assert.match(find(group, "summary")[1].textContent, /Error/);
  assert.deepEqual(find(group, "pre").map((node) => node.textContent), ["request failed"]);
  assert.equal(find(group, "a")[0].href, "/habab-diagnostics");
});

test("empty and malformed calls add no empty panel", () => {
  for (const value of [undefined, [], [{output: "unattributed"}], [null]]) {
    assert.equal(createToolCalls(document, value, strings.ru), undefined);
  }
});

test("client-side output limits remain visible", () => {
  const group = createToolCalls(document, [{name: "bash", status: "completed", output: "x".repeat(13000)}], strings.ru);
  assert.equal(find(group, "pre")[0].textContent.length, 12000);
  assert.equal(find(group, "a").length, 1);
});
