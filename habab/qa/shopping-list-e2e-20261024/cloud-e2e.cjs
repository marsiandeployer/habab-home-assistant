const fs = require("node:fs");
const path = require("node:path");
const puppeteer = require("/root/homeassistent/habab/node_modules/puppeteer-core");

const origin = "https://habab-demo.wpmix.net";
const authPath = "/root/.local/share/habab-owner-test/qa-current-auth.json";
const auth = JSON.parse(fs.readFileSync(authPath, "utf8"));
const outputPath = path.join(__dirname, "cloud-result.json");
const report = {
  target: `${origin}/owner-test → /home/overview`,
  runtime: "habab-owner-test; habab-home:owner-integrated-qa-0.3.4-20261024-v15",
  source: "a3c1d3f06",
  image_sha256: "47c06194a233b0927f31e31176b332c783636717d82d786bb7b5311ccf762c59",
  core: "2026.10.2",
  habab: "0.3.4",
  frontend: "native13 frontend.7",
  checks: [],
};

function save() {
  fs.writeFileSync(outputPath, `${JSON.stringify(report, null, 2)}\n`);
}

function successClaim(text) {
  return /добавил|добавила|добавлено|уже\s+(?:добавил|есть)|готово|внес(?:ен|ла)?/iu.test(text);
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

    const initial = await page.evaluate(async () => {
      const hass = document.querySelector("home-assistant").hass;
      const todoStates = Object.values(hass.states).filter((state) => state.entity_id.startsWith("todo."));
      const registry = await hass.callWS({ type: "config/entity_registry/list" });
      const todoRegistry = registry.filter((entry) => entry.entity_id.startsWith("todo."));
      const statusToken = JSON.parse(localStorage.getItem("hassTokens") || "{}").access_token;
      const statusResponse = await fetch("/api/habab_home/status", {
        headers: { Authorization: `Bearer ${statusToken}` },
      });
      const status = statusResponse.ok ? await statusResponse.json() : {};
      const lists = [];
      for (const state of todoStates) {
        const result = await hass.callWS({ type: "todo/item/list", entity_id: state.entity_id });
        const rows = Array.isArray(result.items) ? result.items : [];
        lists.push({
          entity_id: state.entity_id,
          name: hass.formatEntityName(state),
          item_count: rows.length,
          bread_count: rows.filter((item) => /хлеб/iu.test(String(item.summary || ""))).length,
        });
      }
      const chat = document.querySelector("habab-setup-chat");
      return {
        route: location.pathname,
        owner: hass.user.is_owner,
        core: hass.config.version,
        state: hass.config.state,
        entity_count: Object.keys(hass.states).length,
        todo_component_loaded: hass.config.components.includes("todo"),
        todo_panel_available: Boolean(hass.panels.todo),
        todo_entity_registry_count: todoRegistry.length,
        todo_entities: lists,
        habab_status: { product: status.product, version: status.version, status: status.status },
        chat_session_ready: Boolean(chat.sessionId),
        chat_message_count: Array.isArray(chat.messages) ? chat.messages.length : 0,
      };
    });
    report.checks.push({ name: "authenticated_release_readbefore", ...initial });

    const existingList = initial.todo_entities.find((list) => /покуп/iu.test(list.name)) ||
      (initial.todo_entities.length === 1 ? initial.todo_entities[0] : null);
    if (existingList && existingList.bread_count > 0) {
      report.classification = "NOT_RUN_NO_DUPLICATE";
      report.note = "A bread-like item already exists in the exact selected list; no chat write was sent.";
      save();
      console.log(JSON.stringify(report));
      return;
    }

    const serviceEvents = [];
    await page.evaluate(async () => {
      const hass = document.querySelector("home-assistant").hass;
      window.__shoppingQaEvents = [];
      window.__shoppingQaUnsubscribe = await hass.connection.subscribeEvents((event) => {
        const data = event?.data || {};
        if (data.domain === "todo") {
          window.__shoppingQaEvents.push({
            event_type: event.event_type,
            domain: data.domain,
            service: data.service,
            entity_id: data.service_data?.entity_id || data.target?.entity_id || null,
          });
        }
      }, "call_service");
    });

    await page.evaluate(async () => {
      const chat = document.querySelector("habab-setup-chat");
      if (!chat?.sessionId || chat.busy) throw new Error("chat_not_ready");
      if (!chat.q("dialog")?.open) chat.q(".launcher")?.click();
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const composer = chat.q("textarea");
      composer.value = "добавь хлеб в список покупок";
      composer.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        inputType: "insertText",
        data: composer.value,
      }));
      chat.q("form").requestSubmit();
    });

    await page.waitForFunction(() => {
      const chat = document.querySelector("habab-setup-chat");
      return chat && !chat.busy && chat.messages?.length >= 2 &&
        chat.messages.at(-2)?.role === "user" &&
        chat.messages.at(-2)?.text?.trim() === "добавь хлеб в список покупок" &&
        chat.messages.at(-1)?.role === "assistant";
    }, { timeout: 240000 });
    await new Promise((resolve) => setTimeout(resolve, 1000));

    const turn = await page.evaluate(() => {
      const chat = document.querySelector("habab-setup-chat");
      const answer = String(chat.messages.at(-1)?.text || "");
      const events = [...(window.__shoppingQaEvents || [])];
      window.__shoppingQaUnsubscribe?.();
      return {
        user_visible_chat_turn_complete: true,
        assistant_success_claim: /добавил|добавила|добавлено|уже\s+(?:добавил|есть)|готово|внес(?:ен|ла)?/iu.test(answer),
        assistant_failure_claim: /не удалось|ошибка|не добавил|не могу|нет списка|список не найден/iu.test(answer),
        todo_service_events: events,
      };
    });
    report.checks.push({ name: "user_visible_chat_turn", ...turn });

    if (existingList) {
      const afterChat = await page.evaluate(async (entityId) => {
        const hass = document.querySelector("home-assistant").hass;
        const response = await hass.callWS({ type: "todo/item/list", entity_id: entityId });
        const rows = Array.isArray(response.items) ? response.items : [];
        return {
          entity_id: entityId,
          item_count: rows.length,
          bread_count: rows.filter((item) => /хлеб/iu.test(String(item.summary || ""))).length,
        };
      }, existingList.entity_id);
      report.checks.push({ name: "persistent_todo_readback_after_chat", ...afterChat });

      const uiPage = await context.newPage();
      await uiPage.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
      await uiPage.evaluateOnNewDocument((base, tokens) => {
        window.__qaFindAll = function findAll(selector, root = document) {
          const found = [...root.querySelectorAll(selector)];
          for (const element of root.querySelectorAll("*")) {
            if (element.shadowRoot) found.push(...findAll(selector, element.shadowRoot));
          }
          return found;
        };
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
      await uiPage.goto(`${origin}/owner-test`, { waitUntil: "networkidle2", timeout: 60000 });
      await uiPage.goto(`${origin}/todo?entity_id=${encodeURIComponent(existingList.entity_id)}`, {
        waitUntil: "networkidle2",
        timeout: 90000,
      });
      await uiPage.waitForFunction(() => {
        const hass = document.querySelector("home-assistant")?.hass;
        return hass?.user?.is_owner && document.querySelector("ha-panel-todo");
      }, { timeout: 60000 });
      await uiPage.waitForFunction((entityId) => {
        const panel = document.querySelector("ha-panel-todo");
        const find = (selector, root) => {
          const matches = [...root.querySelectorAll(selector)];
          for (const element of root.querySelectorAll("*")) {
            if (element.shadowRoot) matches.push(...find(selector, element.shadowRoot));
          }
          return matches;
        };
        return find("hui-todo-list-card", panel.shadowRoot).some((card) =>
          card.config?.entity === entityId && Array.isArray(card._items));
      }, { timeout: 45000 }, existingList.entity_id);
      const ui = await uiPage.evaluate((entityId) => {
        const panel = document.querySelector("ha-panel-todo");
        const find = (selector, root) => {
          const matches = [...root.querySelectorAll(selector)];
          for (const element of root.querySelectorAll("*")) {
            if (element.shadowRoot) matches.push(...find(selector, element.shadowRoot));
          }
          return matches;
        };
        const cards = find("hui-todo-list-card", panel.shadowRoot).filter((card) =>
          card.config?.entity === entityId);
        const rows = cards.flatMap((card) => card._items || []);
        const visible = find(".summary", panel.shadowRoot).map((element) => element.textContent.trim());
        return {
          route: location.pathname,
          target_entity_selected: cards.length > 0,
          loaded_item_count: rows.length,
          displayed_bread_count: visible.filter((text) => /хлеб/iu.test(text)).length,
        };
      }, existingList.entity_id);
      report.checks.push({ name: "todo_panel_after_reload", ...ui });
      const afterReload = await uiPage.evaluate(async (entityId) => {
        const hass = document.querySelector("home-assistant").hass;
        const response = await hass.callWS({ type: "todo/item/list", entity_id: entityId });
        const rows = Array.isArray(response.items) ? response.items : [];
        return {
          entity_id: entityId,
          item_count: rows.length,
          bread_count: rows.filter((item) => /хлеб/iu.test(String(item.summary || ""))).length,
        };
      }, existingList.entity_id);
      report.checks.push({ name: "persistent_todo_readback_after_ui_reload", ...afterReload });
      const addCallConfirmed = turn.todo_service_events.some((event) =>
        event.domain === "todo" && event.service === "add_item" &&
        event.entity_id === existingList.entity_id);
      report.checks.push({ name: "todo_add_service_event", confirmed: addCallConfirmed });
      const persistentUiPass = turn.assistant_success_claim && afterChat.bread_count > 0 &&
        afterReload.bread_count > 0 && ui.target_entity_selected && ui.displayed_bread_count > 0;
      report.classification = persistentUiPass
        ? addCallConfirmed ? "CLOUD_POSITIVE_E2E_PASS" : "CLOUD_POSITIVE_E2E_PASS_SERVICE_EVENT_NOT_OBSERVED"
        : "CLOUD_POSITIVE_E2E_INCOMPLETE";
    } else {
      await page.reload({ waitUntil: "networkidle2", timeout: 90000 });
      await page.waitForFunction(() => {
        const hass = document.querySelector("home-assistant")?.hass;
        return hass?.user?.is_owner && hass.connection?.connected;
      }, { timeout: 60000 });
      const afterReload = await page.evaluate(() => {
        const hass = document.querySelector("home-assistant").hass;
        return {
          route: location.pathname,
          entity_count: Object.keys(hass.states).length,
          todo_entity_ids: Object.keys(hass.states).filter((id) => id.startsWith("todo.")),
          todo_component_loaded: hass.config.components.includes("todo"),
        };
      });
      report.checks.push({ name: "ui_reload_no_target_list", ...afterReload });
    }

    if (!existingList && turn.assistant_success_claim && turn.todo_service_events.length === 0) {
      report.classification = "REPRODUCED_FALSE_SUCCESS_WITH_NO_TODO_ENTITY";
    } else if (!existingList && turn.assistant_failure_claim && turn.todo_service_events.length === 0) {
      report.classification = "NO_FALSE_SUCCESS_TARGET_LIST_ABSENT";
    } else if (!existingList) {
      report.classification = "TARGET_LIST_ABSENT_CHAT_RESULT_REQUIRES_REVIEW";
    }
    save();
    console.log(JSON.stringify(report));
  } catch (error) {
    report.classification = "E2E_FAILED";
    report.error = `${error.name}: ${String(error.message).slice(0, 160)}`;
    save();
    console.error(JSON.stringify({ classification: report.classification, error: report.error }));
    process.exitCode = 1;
  } finally {
    await context.close();
    browser.disconnect();
  }
})().catch((error) => {
  report.classification = "E2E_FAILED";
  report.error = `${error.name}: ${String(error.message).slice(0, 160)}`;
  save();
  console.error(JSON.stringify({ classification: report.classification, error: report.error }));
  process.exitCode = 1;
});
