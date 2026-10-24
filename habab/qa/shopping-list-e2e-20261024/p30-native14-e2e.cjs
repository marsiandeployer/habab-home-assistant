const fs = require("node:fs");
const puppeteer = require("/root/homeassistent/habab/node_modules/puppeteer-core");
const { withOwner } = require("/root/.cache/habab-android/owner-auth-qa.cjs");

const base = "http://127.0.0.1:18124"; // Existing parent-owned ADB bridge; this runner never starts or stops it.
const entityId = "todo.shopping_list";
const friendlyName = "Список покупок";
const reportPath = `${__dirname}/p30-native14-result.json`;
const report = {
  target: "P30 VOG-L29; native14 Android app; parent-owned bridge 127.0.0.1:18124",
  runtime_id: "7119388d53035d71",
  source_revision: "9d795359c22cf2844884caa5affc94209a8120ca",
  source_files_matched: 59,
  frontend_payload_files_matched: 14816,
  frontend_version_expected: null,
  entity_id: entityId,
  expected_friendly_name: friendlyName,
  checks: [],
  classification: "PREPARED_NOT_RUN_WAITING_FOR_PHONE_READY",
};

function save() {
  fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
}

async function readList(page) {
  return page.evaluate(async (id) => {
    const hass = document.querySelector("home-assistant")?.hass;
    if (!hass?.user?.is_admin) throw new Error("owner_ui_unavailable");
    const state = hass.states[id];
    if (!state) return { entity_id: id, exists: false, count: 0, bread_count: 0, uids: [] };
    const response = await hass.callWS({ type: "todo/item/list", entity_id: id });
    const items = Array.isArray(response.items) ? response.items : [];
    return {
      entity_id: id,
      exists: true,
      friendly_name: state.attributes?.friendly_name || null,
      count: items.length,
      bread_count: items.filter((item) => String(item.summary || "").trim().toLocaleLowerCase() === "хлеб").length,
      uids: items.map((item) => item.uid).filter((uid) => typeof uid === "string"),
    };
  }, entityId);
}

async function getBackendState(page, sessionId) {
  return page.evaluate(async (id) => {
    const hass = document.querySelector("home-assistant")?.hass;
    const result = await hass.callWS({ type: "habab_home/agent/diagnostics", session_id: id });
    return { active: result.active, error: result.error };
  }, sessionId);
}

async function sendVisibleTurn(page, prompt) {
  await page.evaluate((expectedText) => {
    const chat = document.querySelector("habab-setup-chat");
    if (!chat?.sessionId || chat.busy) throw new Error("chat_not_ready");
    if (!chat.__p30OriginalSend) {
      chat.__p30OriginalSend = chat.send;
      chat.send = async function (...args) {
        const probe = window.__p30TurnProbe;
        const text = typeof args[0] === "string" ? args[0].trim() : this.q("textarea")?.value?.trim();
        if (text !== probe?.expected_text) return this.__p30OriginalSend.apply(this, args);
        try {
          return await this.__p30OriginalSend.apply(this, args);
        } finally {
          probe.completed = true;
          probe.finished_at_ms = Date.now();
          probe.elapsed_ms = probe.finished_at_ms - probe.started_at_ms;
        }
      };
    }
    window.__p30TurnProbe = {
      expected_text: expectedText,
      completed: false,
      started_at_ms: Date.now(),
    };
  }, prompt);

  await page.evaluate((text) => {
    const chat = document.querySelector("habab-setup-chat");
    if (!chat?.sessionId || chat.busy) throw new Error("chat_not_ready");
    if (!chat.q("dialog")?.open) chat.q(".launcher")?.click();
    chat.q("textarea").value = text;
    chat.q("textarea").dispatchEvent(new InputEvent("input", {
      bubbles: true,
      inputType: "insertText",
      data: text,
    }));
    chat.q("form").requestSubmit();
  }, prompt);

  let waitResult = "chat_send_wrapper";
  try {
    await page.waitForFunction(() => {
      const chat = document.querySelector("habab-setup-chat");
      return window.__p30TurnProbe?.completed && !chat?.busy;
    }, { timeout: 180000 });
  } catch {
    waitResult = "terminal_snapshot_after_wrapper_timeout";
  }

  const turn = await page.evaluate(async (text) => {
    const chat = document.querySelector("habab-setup-chat");
    const messages = chat?.messages || [];
    const userIndex = messages.findLastIndex((message) => message.role === "user" && message.text?.trim() === text);
    const nextUserIndex = messages.findIndex((message, index) => index > userIndex && message.role === "user");
    const assistant = messages.slice(userIndex + 1, nextUserIndex < 0 ? undefined : nextUserIndex)
      .findLast((message) => message.role === "assistant");
    const result = await chat.hass.callWS({ type: "habab_home/agent/diagnostics", session_id: chat.sessionId });
    const events = window.__p30TodoEvents || [];
    return {
      exact_user_turn_visible: userIndex >= 0,
      assistant_answer: assistant?.text || "",
      backend: { active: result.active, error: result.error },
      elapsed_ms: window.__p30TurnProbe?.elapsed_ms ?? null,
      todo_service_events: events.map((event) => ({
        domain: event.domain,
        service: event.service,
        entity_ids: event.entity_ids,
      })),
    };
  }, prompt);

  const terminal = turn.exact_user_turn_visible && Boolean(turn.assistant_answer) && turn.backend.active === false;
  return { ...turn, terminal, wait_result: waitResult };
}

async function reloadTodoUi(context) {
  const page = await context.newPage();
  await page.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
  await page.goto(`${base}/todo?entity_id=${encodeURIComponent(entityId)}`, {
    waitUntil: "domcontentloaded",
    timeout: 45000,
  });
  await page.waitForFunction((id) => {
    const hass = document.querySelector("home-assistant")?.hass;
    const panels = [];
    const walk = (root) => {
      for (const element of root.querySelectorAll("*")) {
        if (element.localName === "ha-panel-todo") panels.push(element);
        if (element.shadowRoot) walk(element.shadowRoot);
      }
    };
    walk(document);
    return hass?.user?.is_admin && panels.some((panel) => panel._entityId === id);
  }, { timeout: 60000 }, entityId);
  await page.waitForFunction((id) => {
    const panels = [];
    const walk = (root) => {
      for (const element of root.querySelectorAll("*")) {
        if (element.localName === "ha-panel-todo") panels.push(element);
        if (element.shadowRoot) walk(element.shadowRoot);
      }
    };
    walk(document);
    return panels.some((panel) => {
      if (!panel.shadowRoot) return false;
      const cards = [];
      const findCards = (root) => {
        for (const element of root.querySelectorAll("*")) {
          if (element.localName === "hui-todo-list-card") cards.push(element);
          if (element.shadowRoot) findCards(element.shadowRoot);
        }
      };
      findCards(panel.shadowRoot);
      return panel._entityId === id && cards.some((card) => {
        const config = card._config || card.config;
        return (config?.entity === id || config?.actual_entity_id === id) && Array.isArray(card._items);
      });
    });
  }, { timeout: 45000 }, entityId);

  const ui = await page.evaluate((id) => {
    const panels = [];
    const walk = (root) => {
      for (const element of root.querySelectorAll("*")) {
        if (element.localName === "ha-panel-todo") panels.push(element);
        if (element.shadowRoot) walk(element.shadowRoot);
      }
    };
    walk(document);
    const panel = panels.find((entry) => entry._entityId === id);
    const cards = [];
    const findCards = (root) => {
      for (const element of root.querySelectorAll("*")) {
        if (element.localName === "hui-todo-list-card") cards.push(element);
        if (element.shadowRoot) findCards(element.shadowRoot);
      }
    };
    findCards(panel.shadowRoot);
    const matching = cards.filter((card) => {
      const config = card._config || card.config;
      return config?.entity === id || config?.actual_entity_id === id;
    });
    const items = matching.flatMap((card) => Array.isArray(card._items) ? card._items : []);
    return {
      route: location.pathname,
      panel_entity_id: panel._entityId,
      target_entity_selected: matching.length > 0,
      items_loaded: matching.some((card) => Array.isArray(card._items)),
      card_item_count: items.length,
      card_bread_count: items.filter((item) => String(item.summary || "").trim().toLocaleLowerCase() === "хлеб").length,
    };
  }, entityId);
  const api = await readList(page);
  await page.close();
  return {
    ...ui,
    independent_api_count: api.count,
    independent_api_bread_count: api.bread_count,
  };
}

withOwner(async ({ authData }) => {
  let browser;
  let context;
  try {
    browser = await puppeteer.connect({ browserURL: "http://127.0.0.1:9222", defaultViewport: null });
    context = await browser.createBrowserContext();
    const page = await context.newPage();
    await page.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
    await page.evaluateOnNewDocument((origin, data) => {
      if (location.origin === origin) {
        localStorage.setItem("hassTokens", JSON.stringify(data));
        localStorage.setItem("selectedLanguage", JSON.stringify("ru"));
      }
    }, base, authData);
    await page.goto(`${base}/`, { waitUntil: "domcontentloaded", timeout: 45000 });
    try {
      await page.waitForFunction(() => {
      const hass = document.querySelector("home-assistant")?.hass;
      const chat = document.querySelector("habab-setup-chat");
      return hass?.user?.is_admin && hass.connection?.connected && chat?.sessionId && !chat.busy;
      }, { timeout: 30000 });
    } catch {
      const readiness = await page.evaluate(async () => {
        const hass = document.querySelector("home-assistant")?.hass;
        const chat = document.querySelector("habab-setup-chat");
        const statusText = chat?.q?.("#status")?.textContent?.trim() || "";
        const statusEntry = Object.entries(chat?.t || {}).find(([, value]) => value === statusText);
        const state = {
          home_assistant_present: Boolean(hass),
          admin: Boolean(hass?.user?.is_admin),
          websocket_connected: Boolean(hass?.connection?.connected),
          chat_present: Boolean(chat),
          session_id_present: Boolean(chat?.sessionId),
          busy: typeof chat?.busy === "boolean" ? chat.busy : null,
          status_key: statusEntry?.[0] || (statusText ? "unmapped_status" : "empty"),
          status_is_error: Boolean(chat?.q?.("#status")?.classList.contains("error")),
        };
        if (state.admin && !state.session_id_present) {
          try {
            const result = await hass.callWS({ type: "habab_home/agent/session", new: false });
            state.session_probe = {
              completed: true,
              session_id_present: Boolean(result?.session_id),
              message_count: Array.isArray(result?.messages) ? result.messages.length : null,
              error_code: null,
            };
          } catch (error) {
            state.session_probe = {
              completed: false,
              session_id_present: false,
              message_count: null,
              error_code: typeof error?.code === "string" ? error.code : "other",
            };
          }
        }
        return state;
      });
      report.readiness_before_mutation = readiness;
      report.classification = readiness.session_id_present
        ? "UI_SESSION_NOT_READY_DIRECT_SESSION_PROBE_SUCCEEDED_NO_PROMPT"
        : "CHAT_SESSION_INITIALIZATION_FAILED_NO_PROMPT";
      report.prompt_sent = false;
      report.list_mutated = false;
      save();
      console.log(JSON.stringify({ classification: report.classification, readiness }));
      return;
    }

    report.readiness_before_mutation = await page.evaluate(() => {
      const hass = document.querySelector("home-assistant")?.hass;
      const chat = document.querySelector("habab-setup-chat");
      const statusText = chat?.q?.("#status")?.textContent?.trim() || "";
      const statusEntry = Object.entries(chat?.t || {}).find(([, value]) => value === statusText);
      return {
        home_assistant_present: Boolean(hass),
        admin: Boolean(hass?.user?.is_admin),
        websocket_connected: Boolean(hass?.connection?.connected),
        chat_present: Boolean(chat),
        session_id_present: Boolean(chat?.sessionId),
        busy: typeof chat?.busy === "boolean" ? chat.busy : null,
        status_key: statusEntry?.[0] || (statusText ? "unmapped_status" : "empty"),
      };
    });

    const runtime = await page.evaluate(async () => {
      const hass = document.querySelector("home-assistant").hass;
      const chat = document.querySelector("habab-setup-chat");
      const accessToken = JSON.parse(localStorage.getItem("hassTokens") || "{}").access_token;
      const response = await fetch("/api/habab_home/status", {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const status = response.ok ? await response.json() : {};
      const state = hass.states["todo.shopping_list"];
      return {
        core: hass.config.version,
        habab: status.version || null,
        habab_status: status.status || null,
        entity_exists: Boolean(state),
        friendly_name: state?.attributes?.friendly_name || null,
        chat_ready: Boolean(chat?.sessionId),
      };
    });
    report.live_status = runtime;
    if (runtime.core !== "2026.10.2" || !runtime.entity_exists || runtime.friendly_name !== friendlyName) {
      report.classification = "ABORT_TARGET_MISMATCH_NO_CHAT_WRITE";
      report.readbefore = { entity_id: entityId, exists: runtime.entity_exists, friendly_name: runtime.friendly_name };
      save();
      console.log(JSON.stringify({ classification: report.classification, runtime }));
      return;
    }

    const before = await readList(page);
    report.readbefore = {
      entity_id: entityId,
      friendly_name: before.friendly_name,
      count: before.count,
      bread_count: before.bread_count,
      uid_count: before.uids.length,
    };
    if (!before.exists || before.friendly_name !== friendlyName) {
      report.classification = "ABORT_TARGET_MISMATCH_NO_CHAT_WRITE";
      save();
      console.log(JSON.stringify({ classification: report.classification, readbefore: report.readbefore }));
      return;
    }

    await page.evaluate(async () => {
      const hass = document.querySelector("home-assistant").hass;
      window.__p30TodoEvents = [];
      window.__p30TodoUnsubscribe = await hass.connection.subscribeEvents((event) => {
        const data = event?.data || {};
        if (data.domain !== "todo") return;
        const raw = data.service_data?.entity_id ?? data.target?.entity_id ?? null;
        const entity_ids = Array.isArray(raw) ? raw : raw ? [raw] : [];
        window.__p30TodoEvents.push({ domain: data.domain, service: data.service, entity_ids });
      }, "call_service");
    });

    const prompt = before.bread_count > 0
      ? "Проверь, есть ли хлеб в списке покупок. Ничего не меняй."
      : "добавь хлеб в список покупок";
    report.prompt_kind = before.bread_count > 0 ? "READ_ONLY_EXISTING_BREAD" : "ORIGINAL_ADD_IF_ABSENT";
    const turn = await sendVisibleTurn(page, prompt);
    const after = await readList(page); // Persist API proof before the independent UI reload wait.
    const newUids = after.uids.filter((uid) => !before.uids.includes(uid));
    const preservedUids = before.uids.every((uid) => after.uids.includes(uid));
    const addEventConfirmed = turn.todo_service_events.some((event) =>
      event.domain === "todo" && event.service === "add_item" && event.entity_ids.includes(entityId));
    report.turn = {
      terminal: turn.terminal,
      wait_result: turn.wait_result,
      elapsed_ms: turn.elapsed_ms,
      backend: turn.backend,
      exact_user_turn_visible: turn.exact_user_turn_visible,
      todo_service_events: turn.todo_service_events,
    };
    report.after_chat_api = {
      entity_id: entityId,
      count: after.count,
      bread_count: after.bread_count,
      new_uid_count: newUids.length,
      all_readbefore_uids_preserved: preservedUids,
      add_service_event_confirmed_on_target: addEventConfirmed,
    };
    save();
    console.log(JSON.stringify({ stage: "after_chat_api_readback", after_chat_api: report.after_chat_api,
      terminal: turn.terminal, wait_result: turn.wait_result, elapsed_ms: turn.elapsed_ms,
      backend: turn.backend, service_events: turn.todo_service_events,
      assistant_answer_excerpt: turn.assistant_answer.slice(0, 650) }));

    const expectedCount = before.count + (before.bread_count > 0 ? 0 : 1);
    const expectedBreadCount = before.bread_count > 0 ? before.bread_count : 1;
    if (!turn.terminal) {
      report.classification = "PENDING_OR_TERMINAL_CHAT_NOT_CONFIRMED_NO_RESEND";
      save();
      return;
    }
    if (after.count !== expectedCount || after.bread_count !== expectedBreadCount || !preservedUids ||
        (before.bread_count === 0 && (newUids.length !== 1 || !addEventConfirmed))) {
      report.classification = before.bread_count === 0 ? "REPRODUCED_FALSE_SUCCESS_OR_WRONG_TARGET" : "READ_ONLY_BREAD_PRESENT_STATE_CHANGED";
      save();
      return;
    }

    try {
      const ui = await reloadTodoUi(context);
      report.ui_after_fresh_reload = ui;
      const uiPass = ui.target_entity_selected && ui.items_loaded &&
        ui.card_item_count === after.count && ui.card_bread_count === after.bread_count &&
        ui.independent_api_count === after.count && ui.independent_api_bread_count === after.bread_count;
      report.classification = before.bread_count > 0
        ? uiPass ? "READ_ONLY_BREAD_PRESENT_HONEST_ANSWER_REVIEW" : "READ_ONLY_BREAD_PRESENT_UI_UNCONFIRMED"
        : uiPass ? "P30_NATIVE14_CHAT_SERVICE_PERSISTENCE_UI_PASS_REVIEW_ANSWER" : "P30_NATIVE14_API_PASS_UI_UNCONFIRMED";
    } catch (error) {
      report.ui_after_fresh_reload = { classification: "UI_RELOAD_FAILED", error: `${error.name}: ${String(error.message).slice(0, 120)}` };
      report.classification = before.bread_count > 0 ? "READ_ONLY_BREAD_PRESENT_UI_UNCONFIRMED" : "P30_NATIVE14_API_PASS_UI_UNCONFIRMED";
    }
    await page.evaluate(() => window.__p30TodoUnsubscribe?.());
    save();
  } catch (error) {
    report.classification = "P30_NATIVE14_QA_FAILED_NO_RETRY";
    report.error = `${error.name}: ${String(error.message).slice(0, 160)}`;
    save();
    console.error(JSON.stringify({ classification: report.classification, error: report.error }));
    process.exitCode = 1;
  } finally {
    if (context) await context.close();
    if (browser) browser.disconnect();
  }
}).catch((error) => {
  report.classification = "P30_NATIVE14_AUTH_OR_BRIDGE_FAILED_NO_RETRY";
  report.error = `${error.name}: ${String(error.message).slice(0, 160)}`;
  save();
  console.error(JSON.stringify({ classification: report.classification, error: report.error }));
  process.exitCode = 1;
});
