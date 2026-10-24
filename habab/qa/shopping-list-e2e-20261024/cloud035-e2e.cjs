const fs = require("node:fs");
const puppeteer = require("/root/homeassistent/habab/node_modules/puppeteer-core");

const origin = "https://habab-demo.wpmix.net";
const auth = JSON.parse(fs.readFileSync("/root/.local/share/habab-owner-test/qa-current-auth.json", "utf8"));
const targetEntity = "todo.qa_shopping_list_e2e_20261024_v0_3_5";
const preservedEntity = "todo.qa_shopping_list_e2e_20261024";
const targetList = "QA Shopping List E2E 20261024 v0.3.5";
const reportPath = `${__dirname}/cloud035-result.json`;
const report = {
  target: `${origin}/owner-test → /home/overview`,
  image: "habab-home:owner-gemini-0.3.5-96551e39e",
  image_sha256: "b4a39badcf4cba07073a989ad6f03650eaa33c85c4bda349ab05c7aa17637987",
  source: "96551e39e5ea4b8c3de48e4a47f38d7c330907b8",
  core: "2026.10.2",
  habab: "0.3.5",
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

async function diagnostics(page) {
  return page.evaluate(async () => {
    const chat = document.querySelector("habab-setup-chat");
    const result = await chat.hass.callWS({ type: "habab_home/agent/diagnostics", session_id: chat.sessionId });
    return { active: result.active, error: result.error };
  });
}

async function sendTurn(page, name, prompt, index) {
  await page.evaluate((expectedText) => {
    const chat = document.querySelector("habab-setup-chat");
    if (!chat) throw new Error("chat_send_probe_unavailable");
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
    chat.q("textarea").dispatchEvent(new InputEvent("input", {
      bubbles: true, inputType: "insertText", data: text,
    }));
    chat.q("form").requestSubmit();
  }, prompt);

  try {
    await page.waitForFunction(() => {
      const chat = document.querySelector("habab-setup-chat");
      return chat?.__qaTurnProbe?.completed && !chat.busy;
    }, { timeout: 180000 });
  } catch (error) {
    const snapshot = await page.evaluate(async (text) => {
      const chat = document.querySelector("habab-setup-chat");
      const messages = chat?.messages || [];
      const userIndex = messages.findLastIndex((message) => message.role === "user" && message.text?.trim() === text);
      const nextUserIndex = messages.findIndex((message, index) => index > userIndex && message.role === "user");
      const assistant = messages.slice(userIndex + 1, nextUserIndex < 0 ? undefined : nextUserIndex)
        .findLast((message) => message.role === "assistant");
      const diag = await chat.hass.callWS({ type: "habab_home/agent/diagnostics", session_id: chat.sessionId });
      const events = (window.__todoQaEvents || []).slice(window.__todoQaEventCursor || 0);
      window.__todoQaEventCursor = (window.__todoQaEvents || []).length;
      return {
        exact_user_turn: userIndex >= 0,
        assistant_answer: assistant ? String(assistant.text || "") : null,
        backend: { active: diag.active, error: diag.error },
        turn_probe: chat.__qaTurnProbe,
        todo_service_events: events.map((event) => ({
          domain: event.domain, service: event.service, entity_id: event.entity_id,
        })),
      };
    }, prompt).catch(() => ({ exact_user_turn: false, assistant_answer: null, backend: { active: null, error: "diagnostics_unavailable" }, todo_service_events: [] }));
    const terminalTurn = snapshot.backend.active === false && snapshot.exact_user_turn && snapshot.assistant_answer !== null;
    report.checks.push({
      name,
      complete: terminalTurn,
      wait: terminalTurn ? "timeout_but_turn_terminal" : "timeout",
      backend: snapshot.backend,
      todo_service_events: snapshot.todo_service_events,
    });
    save();
    console.log(JSON.stringify({ name, complete: terminalTurn, backend: snapshot.backend,
      assistant_answer: snapshot.assistant_answer?.slice(0, 700) || null,
      todo_service_events: snapshot.todo_service_events }));
    return { complete: terminalTurn, answer: snapshot.assistant_answer,
      backend: snapshot.backend, todo_service_events: snapshot.todo_service_events };
  }

  await new Promise((resolve) => setTimeout(resolve, 800));
  const turn = await page.evaluate(async () => {
    const chat = document.querySelector("habab-setup-chat");
    const prompt = chat.__qaTurnProbe.expected_text;
    const userIndex = chat.messages.findLastIndex((message) => message.role === "user" && message.text?.trim() === prompt);
    const nextUserIndex = chat.messages.findIndex((message, index) => index > userIndex && message.role === "user");
    const assistant = chat.messages.slice(userIndex + 1, nextUserIndex < 0 ? undefined : nextUserIndex)
      .findLast((message) => message.role === "assistant");
    const events = (window.__todoQaEvents || []).slice(window.__todoQaEventCursor || 0);
    window.__todoQaEventCursor = (window.__todoQaEvents || []).length;
    const result = await chat.hass.callWS({ type: "habab_home/agent/diagnostics", session_id: chat.sessionId });
    return {
      answer: String(assistant?.text || ""),
      exact_user_turn: userIndex >= 0,
      turn_probe: chat.__qaTurnProbe,
      backend: { active: result.active, error: result.error },
      todo_service_events: events.map((event) => ({
        domain: event.domain,
        service: event.service,
        entity_id: event.entity_id,
      })),
    };
  });
  report.checks.push({
    name,
    complete: turn.exact_user_turn && Boolean(turn.answer) && turn.backend.active === false,
    turn_index: index,
    backend: turn.backend,
    todo_service_events: turn.todo_service_events,
  });
  save();
  console.log(JSON.stringify({ name, complete: turn.exact_user_turn && Boolean(turn.answer) && turn.backend.active === false,
    answer: turn.answer.slice(0, 700), elapsed_ms: turn.turn_probe.elapsed_ms, backend: turn.backend,
    todo_service_events: turn.todo_service_events }));
  return { complete: turn.exact_user_turn && Boolean(turn.answer) && turn.backend.active === false,
    answer: turn.answer, backend: turn.backend, todo_service_events: turn.todo_service_events };
}

async function reloadTargetUi(context) {
  const page = await context.newPage();
  await page.setViewport({ width: 1280, height: 800 });
  await page.goto(`${origin}/todo?entity_id=${encodeURIComponent(targetEntity)}`, { waitUntil: "networkidle2", timeout: 90000 });
  await page.waitForFunction((id) => {
    const hass = document.querySelector("home-assistant")?.hass;
    return hass?.user?.is_admin && hass.states?.[id] && location.pathname === "/todo";
  }, { timeout: 60000 }, targetEntity);
  await page.waitForFunction((id) => {
    const panel = document.querySelector("ha-panel-todo");
    if (!panel?.shadowRoot) return false;
    const find = (selector, root) => {
      const found = [...root.querySelectorAll(selector)];
      for (const element of root.querySelectorAll("*")) {
        if (element.shadowRoot) found.push(...find(selector, element.shadowRoot));
      }
      return found;
    };
    const cards = find("hui-todo-list-card", panel.shadowRoot);
    return cards.some((card) => card.config?.entity === id && Array.isArray(card._items));
  }, { timeout: 45000 }, targetEntity);
  const result = await page.evaluate((id) => {
    const panel = document.querySelector("ha-panel-todo");
    const find = (selector, root) => {
      const found = [...root.querySelectorAll(selector)];
      for (const element of root.querySelectorAll("*")) {
        if (element.shadowRoot) found.push(...find(selector, element.shadowRoot));
      }
      return found;
    };
    const cards = find("hui-todo-list-card", panel.shadowRoot)
      .filter((card) => card.config?.entity === id);
    const items = cards.flatMap((card) => card._items || []);
    return {
      route: location.pathname,
      target_entity_selected: cards.length > 0,
      loaded_item_count: items.length,
      displayed_bread_count: items.filter((item) => String(item.summary || "").trim().toLocaleLowerCase() === "хлеб").length,
    };
  }, targetEntity);
  const api = await readTodo(page, targetEntity);
  await page.close();
  return { ...result, api_count_after_reload: api.count, api_bread_count_after_reload: api.bread_count };
}

(async () => {
  const browser = await puppeteer.connect({ browserURL: "http://127.0.0.1:9222", defaultViewport: null });
  const context = await browser.createBrowserContext();
  try {
    const page = await context.newPage();
    await page.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
    await page.evaluateOnNewDocument((base, tokens) => {
      if (location.origin === base) {
        localStorage.setItem("hassTokens", JSON.stringify({
          ...tokens,
          hassUrl: base,
          clientId: tokens.client_id,
          expires: Date.now() + tokens.expires_in * 1000,
        }));
        localStorage.setItem("selectedLanguage", JSON.stringify("ru"));
      }
    }, origin, auth);
    await page.goto(`${origin}/owner-test`, { waitUntil: "networkidle2", timeout: 60000 });
    await page.goto(`${origin}/home/overview`, { waitUntil: "networkidle2", timeout: 60000 });
    await page.waitForFunction(() => {
      const hass = document.querySelector("home-assistant")?.hass;
      const chat = document.querySelector("habab-setup-chat");
      return hass?.user?.is_admin && hass.connection?.connected && chat?.sessionId && !chat.busy;
    }, { timeout: 60000 });

    const runtime = await page.evaluate(async () => {
      const hass = document.querySelector("home-assistant").hass;
      const chat = document.querySelector("habab-setup-chat");
      const token = JSON.parse(localStorage.getItem("hassTokens") || "{}").access_token;
      const response = await fetch("/api/habab_home/status", { headers: { Authorization: `Bearer ${token}` } });
      const status = response.ok ? await response.json() : {};
      const states = Object.keys(hass.states).filter((id) => id.startsWith("todo."));
      return {
        core: hass.config.version,
        habab: status.version,
        state: status.status,
        chat_session_ready: Boolean(chat.sessionId),
        existing_todo_entity_ids: states,
      };
    });
    report.runtime = runtime;
    if (runtime.core !== "2026.10.2" || runtime.habab !== "0.3.5" || runtime.state !== "running") {
      report.classification = "ABORT_WRONG_RUNTIME";
      save();
      console.log(JSON.stringify({ classification: report.classification, runtime }));
      return;
    }

    const [beforeTarget, beforePreserved] = await Promise.all([
      readTodo(page, targetEntity), readTodo(page, preservedEntity),
    ]);
    report.readbefore = {
      target: { entity_id: beforeTarget.entity_id, count: beforeTarget.count, bread_count: beforeTarget.bread_count },
      preserved_v15_fixture: { entity_id: beforePreserved.entity_id, count: beforePreserved.count, bread_count: beforePreserved.bread_count },
    };
    if (beforeTarget.count !== 0 || beforeTarget.bread_count !== 0 || beforePreserved.bread_count !== 1) {
      report.classification = "ABORT_UNEXPECTED_FIXTURE_STATE_NO_CHAT_WRITE";
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
        if (data.domain === "todo") {
          window.__todoQaEvents.push({
            domain: data.domain,
            service: data.service,
            entity_id: data.service_data?.entity_id || data.target?.entity_id || null,
          });
        }
      }, "call_service");
    });

    const original = await sendTurn(page, "original_bread_add", `добавь хлеб в список покупок «${targetList}»`, 1);
    if (!original.complete) {
      report.classification = "PENDING_OR_FAILED_ORIGINAL_TURN";
      save();
      return;
    }
    const afterOriginal = await readTodo(page, targetEntity);
    const afterOriginalPreserved = await readTodo(page, preservedEntity);
    const newUids = afterOriginal.uids.filter((uid) => !beforeTarget.uids.includes(uid));
    const uiAfterOriginal = await reloadTargetUi(context);
    report.checks.push({
      name: "original_add_persistent_readback_and_ui_reload",
      target: { count: afterOriginal.count, bread_count: afterOriginal.bread_count, new_uid_count: newUids.length },
      ui: uiAfterOriginal,
      preserved_v15_fixture: { count: afterOriginalPreserved.count, bread_count: afterOriginalPreserved.bread_count },
    });
    save();
    if (afterOriginal.bread_count !== 1 || newUids.length !== 1 ||
        uiAfterOriginal.displayed_bread_count !== 1 || uiAfterOriginal.api_bread_count_after_reload !== 1 ||
        afterOriginalPreserved.bread_count !== 1) {
      report.classification = "ORIGINAL_ADD_E2E_FAILED_OR_WRONG_TARGET";
      save();
      await page.evaluate(() => window.__todoQaUnsubscribe?.());
      return;
    }

    const readExisting = await sendTurn(page, "existing_item_read_no_duplicate", `Проверь, есть ли хлеб в списке «${targetList}». Только ответь, ничего не меняй.`, 2);
    if (!readExisting.complete) { report.classification = "PENDING_AFTER_READ_INTENT"; save(); return; }
    const afterReadIntent = await readTodo(page, targetEntity);
    report.checks.push({ name: "existing_item_read_no_duplicate_state", count: afterReadIntent.count, bread_count: afterReadIntent.bread_count });
    save();
    if (afterReadIntent.count !== afterOriginal.count || afterReadIntent.bread_count !== 1) {
      report.classification = "READ_INTENT_MUTATED_FIXTURE";
      await page.evaluate(() => window.__todoQaUnsubscribe?.());
      return;
    }

    const intentionalRepeat = await sendTurn(page, "explicit_repeat_allowed", `Добавь ещё один экземпляр хлеба в список «${targetList}».`, 3);
    if (!intentionalRepeat.complete) { report.classification = "PENDING_AFTER_REPEAT_INTENT"; save(); return; }
    const afterRepeat = await readTodo(page, targetEntity);
    const repeatUids = afterRepeat.uids.filter((uid) => !afterOriginal.uids.includes(uid));
    report.checks.push({ name: "explicit_repeat_new_persistent_item", count: afterRepeat.count, bread_count: afterRepeat.bread_count, new_uid_count: repeatUids.length });
    save();
    if (afterRepeat.bread_count !== 2 || repeatUids.length !== 1) {
      report.classification = "EXPLICIT_REPEAT_NOT_CONFIRMED";
      await page.evaluate(() => window.__todoQaUnsubscribe?.());
      return;
    }

    const beforeUnknown = await Promise.all([readTodo(page, targetEntity), readTodo(page, preservedEntity)]);
    const unknownList = await sendTurn(page, "unknown_list_no_invented_mutation", "Добавь корицу в список «QA Несуществующий список 0.3.5», если такой список уже подключён. Если его нет, ничего не создавай, не используй другой список и спроси меня, что делать.", 4);
    if (!unknownList.complete) { report.classification = "PENDING_AFTER_UNKNOWN_LIST_INTENT"; save(); return; }
    const afterUnknown = await Promise.all([readTodo(page, targetEntity), readTodo(page, preservedEntity)]);
    const allStatesAfterUnknown = await page.evaluate(() => Object.keys(document.querySelector("home-assistant")?.hass?.states || {}).filter((id) => id.startsWith("todo.")));
    report.checks.push({
      name: "unknown_list_no_invented_mutation",
      before: beforeUnknown.map(({ entity_id, count, bread_count }) => ({ entity_id, count, bread_count })),
      after: afterUnknown.map(({ entity_id, count, bread_count }) => ({ entity_id, count, bread_count })),
      todo_entity_count: allStatesAfterUnknown.length,
    });
    save();
    if (beforeUnknown.some((entry, i) => entry.count !== afterUnknown[i].count || entry.bread_count !== afterUnknown[i].bread_count) ||
        allStatesAfterUnknown.length !== 2) {
      report.classification = "UNKNOWN_LIST_MUTATED_OR_CREATED_ENTITY";
      await page.evaluate(() => window.__todoQaUnsubscribe?.());
      return;
    }

    const naturalQuestion = await sendTurn(page, "natural_question_no_mutation", "Как ты проверяешь, что пункт действительно появился в выбранном списке?", 5);
    if (!naturalQuestion.complete) { report.classification = "PENDING_AFTER_NATURAL_QUESTION"; save(); return; }
    const afterQuestion = await Promise.all([readTodo(page, targetEntity), readTodo(page, preservedEntity)]);
    report.checks.push({
      name: "natural_question_no_mutation",
      state_unchanged: afterQuestion.every((entry, i) => entry.count === [afterRepeat, afterOriginalPreserved][i].count),
    });
    report.todo_service_events = await page.evaluate(() => {
      window.__todoQaUnsubscribe?.();
      return window.__todoQaEvents || [];
    });
    report.classification = report.checks.every((check) => check.name !== "natural_question_no_mutation" || check.state_unchanged)
      ? "CLOUD_035_CHAT_TODO_E2E_PASS_REVIEW_ANSWERS_MANUALLY"
      : "NATURAL_QUESTION_MUTATED_FIXTURE";
    save();
  } catch (error) {
    report.classification = "CLOUD_035_E2E_FAILED";
    report.error = `${error.name}: ${String(error.message).slice(0, 160)}`;
    save();
    console.error(JSON.stringify({ classification: report.classification, error: report.error }));
    process.exitCode = 1;
  } finally {
    await context.close();
    browser.disconnect();
  }
})().catch((error) => {
  report.classification = "CLOUD_035_E2E_FAILED";
  report.error = `${error.name}: ${String(error.message).slice(0, 160)}`;
  save();
  console.error(JSON.stringify({ classification: report.classification, error: report.error }));
  process.exitCode = 1;
});
