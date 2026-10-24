const fs = require("node:fs");
const puppeteer = require("/root/homeassistent/habab/node_modules/puppeteer-core");

const origin = "https://habab-demo.wpmix.net";
const auth = JSON.parse(fs.readFileSync("/root/.local/share/habab-owner-test/qa-current-auth.json", "utf8"));
const targetEntity = "todo.qa_shopping_list_e2e_20261024_v0_3_5";
const preservedEntity = "todo.qa_shopping_list_e2e_20261024";
const targetList = "QA Shopping List E2E 20261024 v0.3.5";
const reportPath = `${__dirname}/cloud035-v2-risk-result.json`;
const report = {
  target: `${origin}/owner-test → existing canonical chat`,
  image: "habab-home:owner-gemini-0.3.5-9d795359c",
  image_sha256: "e08f185758287f824bb46eb9c955f2605b10648807939189da663dcbd0dbdd29",
  source: "9d795359c22cf2844884caa5affc94209a8120ca",
  fixture_entity: targetEntity,
  preserved_v15_fixture_entity: preservedEntity,
  checks: [],
};

function save() {
  fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
}

async function readTodo(page, entityId) {
  return page.evaluate(async (id) => {
    const hass = document.querySelector("home-assistant")?.hass;
    if (!hass?.user?.is_admin) throw new Error("owner_ui_unavailable");
    const response = await hass.callWS({ type: "todo/item/list", entity_id: id });
    const items = Array.isArray(response.items) ? response.items : [];
    return {
      entity_id: id,
      count: items.length,
      bread_count: items.filter((item) => String(item.summary || "").trim().toLocaleLowerCase() === "хлеб").length,
      uids: items.map((item) => item.uid).filter((uid) => typeof uid === "string"),
    };
  }, entityId);
}

async function sendTurn(page, name, prompt, index) {
  await page.evaluate((expectedText) => {
    const chat = document.querySelector("habab-setup-chat");
    if (!chat) throw new Error("chat_not_ready");
    if (!chat.__qaOriginalSend) {
      chat.__qaOriginalSend = chat.send;
      chat.send = async function (...args) {
        const text = typeof args[0] === "string" ? args[0].trim() : this.q("textarea")?.value?.trim();
        if (text !== this.__qaTurnProbe?.expected_text) return this.__qaOriginalSend.apply(this, args);
        try {
          return await this.__qaOriginalSend.apply(this, args);
        } finally {
          this.__qaTurnProbe.completed = true;
          this.__qaTurnProbe.finished_at = Date.now();
          this.__qaTurnProbe.elapsed_ms = this.__qaTurnProbe.finished_at - this.__qaTurnProbe.started_at;
        }
      };
    }
    chat.__qaTurnProbe = { expected_text: expectedText, completed: false, started_at: Date.now() };
  }, prompt);
  await page.evaluate((text) => {
    const chat = document.querySelector("habab-setup-chat");
    if (!chat?.sessionId || chat.busy) throw new Error("chat_not_ready");
    if (!chat.q("dialog")?.open) chat.q(".launcher")?.click();
    chat.q("textarea").value = text;
    chat.q("textarea").dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    chat.q("form").requestSubmit();
  }, prompt);

  try {
    await page.waitForFunction(() => {
      const chat = document.querySelector("habab-setup-chat");
      return chat?.__qaTurnProbe?.completed && !chat.busy;
    }, { timeout: 180000 });
  } catch {
    const current = await page.evaluate(async (text) => {
      const chat = document.querySelector("habab-setup-chat");
      const d = await chat.hass.callWS({ type: "habab_home/agent/diagnostics", session_id: chat.sessionId });
      const messages = chat.messages || [];
      const userIndex = messages.findLastIndex((message) => message.role === "user" && message.text?.trim() === text);
      const answer = messages.slice(userIndex + 1).findLast((message) => message.role === "assistant");
      return { backend: { active: d.active, error: d.error }, exact_user_turn: userIndex >= 0,
        assistant_answer: answer?.text || null,
        elapsed_ms: chat.__qaTurnProbe?.elapsed_ms || null };
    }, prompt).catch(() => ({ backend: { active: null, error: "diagnostics_unavailable" }, exact_user_turn: false,
      assistant_answer: null, elapsed_ms: null }));
    const complete = current.backend.active === false && current.exact_user_turn && typeof current.assistant_answer === "string";
    report.checks.push({ name, complete, wait: complete ? "timeout_but_terminal" : "timeout", backend: current.backend,
      elapsed_ms: current.elapsed_ms });
    save();
    console.log(JSON.stringify({ name, complete, elapsed_ms: current.elapsed_ms, backend: current.backend,
      answer: current.assistant_answer?.slice(0, 650) || null }));
    return { complete, ...current, events: [] };
  }

  const current = await page.evaluate(async () => {
    const chat = document.querySelector("habab-setup-chat");
    const promptText = chat.__qaTurnProbe.expected_text;
    const messages = chat.messages || [];
    const userIndex = messages.findLastIndex((message) => message.role === "user" && message.text?.trim() === promptText);
    const nextUserIndex = messages.findIndex((message, index) => index > userIndex && message.role === "user");
    const answer = messages.slice(userIndex + 1, nextUserIndex < 0 ? undefined : nextUserIndex)
      .findLast((message) => message.role === "assistant");
    const d = await chat.hass.callWS({ type: "habab_home/agent/diagnostics", session_id: chat.sessionId });
    const events = (window.__todoQaEvents || []).slice(window.__todoQaEventCursor || 0);
    window.__todoQaEventCursor = (window.__todoQaEvents || []).length;
    return {
      exact_user_turn: userIndex >= 0,
      assistant_answer: answer?.text || "",
      elapsed_ms: chat.__qaTurnProbe.elapsed_ms,
      backend: { active: d.active, error: d.error },
      events: events.map((event) => ({ domain: event.domain, service: event.service, entity_id: event.entity_id })),
    };
  });
  const complete = current.exact_user_turn && Boolean(current.assistant_answer) && current.backend.active === false;
  report.checks.push({ name, complete, turn_index: index, elapsed_ms: current.elapsed_ms,
    backend: current.backend, todo_service_events: current.events });
  save();
  console.log(JSON.stringify({ name, complete, elapsed_ms: current.elapsed_ms, backend: current.backend,
    answer: current.assistant_answer.slice(0, 650), todo_service_events: current.events }));
  return { complete, ...current };
}

(async () => {
  const browser = await puppeteer.connect({ browserURL: "http://127.0.0.1:9222", defaultViewport: null });
  const context = await browser.createBrowserContext();
  try {
    const page = await context.newPage();
    await page.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
    await page.evaluateOnNewDocument((base, tokens) => {
      if (location.origin === base) localStorage.setItem("hassTokens", JSON.stringify({
        ...tokens, hassUrl: base, clientId: tokens.client_id, expires: Date.now() + tokens.expires_in * 1000,
      }));
    }, origin, auth);
    await page.goto(`${origin}/owner-test`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.goto(`${origin}/home/overview`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForFunction(() => {
      const hass = document.querySelector("home-assistant")?.hass;
      const chat = document.querySelector("habab-setup-chat");
      return hass?.user?.is_admin && hass.connection?.connected && chat?.sessionId && !chat.busy;
    }, { timeout: 60000 });

    const [targetBefore, preservedBefore] = await Promise.all([
      readTodo(page, targetEntity), readTodo(page, preservedEntity),
    ]);
    report.readbefore = {
      target: { entity_id: targetEntity, count: targetBefore.count, bread_count: targetBefore.bread_count },
      preserved: { entity_id: preservedEntity, count: preservedBefore.count, bread_count: preservedBefore.bread_count },
    };
    if (targetBefore.count !== 1 || targetBefore.bread_count !== 1 ||
        preservedBefore.count !== 2 || preservedBefore.bread_count !== 2) {
      report.classification = "ABORT_UNEXPECTED_READBEFORE_NO_PROMPTS";
      save();
      console.log(JSON.stringify({ classification: report.classification, readbefore: report.readbefore }));
      return;
    }

    await page.evaluate(async () => {
      const hass = document.querySelector("home-assistant").hass;
      window.__todoQaEvents = [];
      window.__todoQaEventCursor = 0;
      window.__todoQaUnsubscribe = await hass.connection.subscribeEvents((event) => {
        const data = event?.data || {};
        if (data.domain === "todo") window.__todoQaEvents.push({
          domain: data.domain,
          service: data.service,
          entity_id: data.service_data?.entity_id || data.target?.entity_id || null,
        });
      }, "call_service");
    });

    const readExisting = await sendTurn(page, "existing_item_read_no_duplicate", `Проверь, есть ли хлеб в списке «${targetList}». Только ответь, ничего не меняй.`, 2);
    if (!readExisting.complete) { report.classification = "PENDING_EXISTING_ITEM_READ"; save(); return; }
    const afterRead = await Promise.all([readTodo(page, targetEntity), readTodo(page, preservedEntity)]);
    report.checks.push({ name: "existing_item_read_no_duplicate_state",
      target_count: afterRead[0].count, target_bread_count: afterRead[0].bread_count,
      preserved_count: afterRead[1].count, preserved_bread_count: afterRead[1].bread_count,
      unchanged: afterRead[0].uids.join("|") === targetBefore.uids.join("|") &&
        afterRead[1].uids.join("|") === preservedBefore.uids.join("|") });
    save();
    if (!report.checks.at(-1).unchanged) { report.classification = "READ_INTENT_MUTATED_LIST"; save(); return; }

    const repeat = await sendTurn(page, "explicit_repeat_allowed", `Добавь ещё один экземпляр хлеба в список «${targetList}».`, 3);
    if (!repeat.complete) { report.classification = "PENDING_EXPLICIT_REPEAT"; save(); return; }
    const afterRepeat = await readTodo(page, targetEntity);
    const repeatUids = afterRepeat.uids.filter((uid) => !targetBefore.uids.includes(uid));
    const oldAfterRepeat = await readTodo(page, preservedEntity);
    report.checks.push({ name: "explicit_repeat_new_item",
      target_count: afterRepeat.count, target_bread_count: afterRepeat.bread_count, new_uid_count: repeatUids.length,
      preserved_count: oldAfterRepeat.count, preserved_bread_count: oldAfterRepeat.bread_count });
    save();
    if (afterRepeat.bread_count !== 2 || repeatUids.length !== 1 ||
        oldAfterRepeat.count !== preservedBefore.count || oldAfterRepeat.bread_count !== preservedBefore.bread_count) {
      report.classification = "EXPLICIT_REPEAT_WRONG_TARGET_OR_UNVERIFIED"; save(); return;
    }

    const unknown = await sendTurn(page, "unknown_list_no_invented_mutation",
      "Добавь корицу в список с точным названием «QA Несуществующий список 0.3.5». Если такого списка нет, ничего не создавай, не добавляй и не используй другой список — сначала спроси меня.", 4);
    if (!unknown.complete) { report.classification = "PENDING_UNKNOWN_LIST"; save(); return; }
    const afterUnknown = await Promise.all([readTodo(page, targetEntity), readTodo(page, preservedEntity)]);
    const todoEntitiesAfterUnknown = await page.evaluate(() => Object.keys(document.querySelector("home-assistant")?.hass?.states || {})
      .filter((id) => id.startsWith("todo.")));
    report.checks.push({ name: "unknown_list_no_invented_mutation",
      target_count: afterUnknown[0].count, target_bread_count: afterUnknown[0].bread_count,
      preserved_count: afterUnknown[1].count, preserved_bread_count: afterUnknown[1].bread_count,
      todo_entity_count: todoEntitiesAfterUnknown.length,
      unchanged: afterUnknown[0].uids.join("|") === afterRepeat.uids.join("|") &&
        afterUnknown[1].uids.join("|") === oldAfterRepeat.uids.join("|") && todoEntitiesAfterUnknown.length === 2 });
    save();
    if (!report.checks.at(-1).unchanged) { report.classification = "UNKNOWN_LIST_MUTATED_OR_CREATED"; save(); return; }

    const natural = await sendTurn(page, "natural_question_no_mutation",
      "Как ты проверяешь, что пункт действительно появился в выбранном списке?", 5);
    if (!natural.complete) { report.classification = "PENDING_NATURAL_QUESTION"; save(); return; }
    const afterNatural = await Promise.all([readTodo(page, targetEntity), readTodo(page, preservedEntity)]);
    report.checks.push({ name: "natural_question_no_mutation",
      unchanged: afterNatural[0].uids.join("|") === afterRepeat.uids.join("|") &&
        afterNatural[1].uids.join("|") === oldAfterRepeat.uids.join("|") });
    report.todo_service_events = await page.evaluate(() => {
      window.__todoQaUnsubscribe?.();
      return window.__todoQaEvents || [];
    });
    report.classification = report.checks.at(-1).unchanged
      ? "CLOUD_035_V2_RISK_INTENTS_PASS_REVIEW_ANSWERS_MANUALLY"
      : "NATURAL_QUESTION_MUTATED_LIST";
    save();
  } catch (error) {
    report.classification = "CLOUD_035_V2_RISK_FAILED";
    report.error = `${error.name}: ${String(error.message).slice(0, 160)}`;
    save();
    console.error(JSON.stringify({ classification: report.classification, error: report.error }));
    process.exitCode = 1;
  } finally {
    await context.close();
    browser.disconnect();
  }
})().catch((error) => {
  report.classification = "CLOUD_035_V2_RISK_FAILED";
  report.error = `${error.name}: ${String(error.message).slice(0, 160)}`;
  save();
  console.error(JSON.stringify({ classification: report.classification, error: report.error }));
  process.exitCode = 1;
});
