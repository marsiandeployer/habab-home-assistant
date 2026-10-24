"use strict";

const assert = require("node:assert/strict");
const { hasAcceptedTurn } = require("./service-call-e2e.cjs");

const prompt = "Turn on the disposable QA helper";
const cappedTail = Array.from({ length: 58 }, (_, index) => ({ role: index % 2 ? "assistant" : "user", text: `old ${index}` }));
cappedTail.push({ role: "user", text: prompt }, { role: "assistant", text: "The helper is on." });
const busyCycle = [{ busy: true }, { busy: false }];

assert.equal(cappedTail.length, 60);
assert.equal(hasAcceptedTurn(cappedTail, prompt, busyCycle), true, "accept completed pair at 60-item cap");
assert.equal(hasAcceptedTurn(cappedTail, prompt, []), false, "reject identical preexisting pair when submit was skipped");
assert.equal(hasAcceptedTurn(cappedTail, prompt, [{ busy: true }]), false, "reject while turn has not settled");
assert.equal(hasAcceptedTurn(cappedTail, "different prompt", busyCycle), false, "reject unrelated user/assistant tail");
assert.equal(hasAcceptedTurn(cappedTail.slice(0, -1), prompt, busyCycle), false, "reject missing assistant reply");

process.stdout.write("completion correlation synthetic checks passed\n");
