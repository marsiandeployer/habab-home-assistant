"use strict";

const assert = require("node:assert/strict");
const { hasAcceptedTurn, shouldDeferFixtureCleanup } = require("./service-call-e2e.cjs");

const prompt = "Turn on the disposable QA helper";
const cappedTail = Array.from({ length: 58 }, (_, index) => ({ role: index % 2 ? "assistant" : "user", text: `old ${index}` }));
cappedTail.push({ role: "user", text: prompt }, { role: "assistant", text: "The helper is on." });
const completedProbe = { expected_text: prompt, submitted_text: prompt, accepted: true,
  completed: true, final_busy: false, status_error: false };
const idleUi = { busy: false, status_error: false };

assert.equal(cappedTail.length, 60);
assert.equal(hasAcceptedTurn(cappedTail, prompt, completedProbe, idleUi), true, "accept completed pair at 60-item cap");
assert.equal(hasAcceptedTurn(cappedTail, prompt, { ...completedProbe, accepted: false }, idleUi), false,
  "reject identical preexisting pair when submit was skipped");
assert.equal(hasAcceptedTurn(cappedTail, prompt, { ...completedProbe, completed: false }, idleUi), false,
  "reject unfinished turn");
assert.equal(hasAcceptedTurn(cappedTail, prompt, { ...completedProbe, final_busy: true }, idleUi), false,
  "reject a turn still busy");
assert.equal(hasAcceptedTurn(cappedTail, prompt, { ...completedProbe, submitted_text: "other prompt" }, idleUi), false,
  "reject a probe for another prompt");
assert.equal(hasAcceptedTurn(cappedTail, prompt, { ...completedProbe, status_error: true }, idleUi), false,
  "reject a turn ending in UI error");
assert.equal(hasAcceptedTurn(cappedTail, prompt, completedProbe, { busy: true, status_error: false }), false,
  "reject current UI still busy");
assert.equal(hasAcceptedTurn(cappedTail, prompt, completedProbe, { busy: false, status_error: true }), false,
  "reject current UI error");
assert.equal(hasAcceptedTurn(cappedTail, "different prompt", completedProbe, idleUi), false,
  "reject unrelated user/assistant tail");
assert.equal(hasAcceptedTurn(cappedTail.slice(0, -1), prompt, completedProbe, idleUi), false,
  "reject missing assistant reply");

assert.equal(shouldDeferFixtureCleanup(true, true), true, "retain fixture while backend reports active");
assert.equal(shouldDeferFixtureCleanup(true, null), true, "retain fixture when backend state is unknown");
assert.equal(shouldDeferFixtureCleanup(true, false), false, "allow cleanup after backend idle confirmation");
assert.equal(shouldDeferFixtureCleanup(false, null), false, "no active turn guard when no turn may be running");

process.stdout.write("completion correlation synthetic checks passed\n");
