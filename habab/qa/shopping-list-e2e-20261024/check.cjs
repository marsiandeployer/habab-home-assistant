const fs = require("node:fs");
const path = require("node:path");
const puppeteer = require("/root/.nvm/versions/node/v22.21.1/lib/node_modules/puppeteer");
const { withOwner } = require("/root/.cache/habab-android/owner-auth-qa.cjs");

const base = "http://127.0.0.1:18124";
const entityId = "todo.shopping_list";
const outputDir = __dirname;
const report = {
  target: "P30 VOG-L29 via ADB HTTP bridge; authenticated owner UI",
  core: "2026.10.2",
  habab: "0.3.4",
  frontend: "native13 frontend.7",
  entity_id: entityId,
  checks: [],
};

function save() {
  fs.writeFileSync(path.join(outputDir, "result.json"), `${JSON.stringify(report, null, 2)}\n`);
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
      if (location.origin === origin) localStorage.setItem("hassTokens", JSON.stringify(data));
    }, base, authData);
    await page.goto(`${base}/`, { waitUntil: "networkidle2", timeout: 120000 });
    await page.waitForFunction(() => {
      const hass = document.querySelector("home-assistant")?.hass;
      const chat = document.querySelector("habab-setup-chat");
      return hass?.user?.is_owner && chat?.sessionId && !chat.busy;
    }, { timeout: 60000 });

    const getItems = () => page.evaluate(async (id) => {
      const hass = document.querySelector("home-assistant")?.hass;
      if (!hass?.user?.is_owner) throw new Error("owner_ui_unavailable");
      const response = await hass.callWS({ type: "todo/item/list", entity_id: id });
      const items = Array.isArray(response?.items) ? response.items : [];
      const summaries = items.map((item) => String(item.summary || "").trim());
      return {
        count: items.length,
        bread_count: summaries.filter((value) => /хлеб/iu.test(value)).length,
      };
    }, entityId);

    const baseline = await getItems();
    report.checks.push({ name: "readbefore_exact_todo_list", ...baseline });

    const chatIdentity = await page.evaluate(() => {
      const chat = document.querySelector("habab-setup-chat");
      return {
        channel: "web-chat",
        session_ready: Boolean(chat?.sessionId),
      };
    });
    report.checks.push({ name: "existing_web_chat", ...chatIdentity });

    if (baseline.bread_count > 0) {
      report.classification = "NOT_RUN_NO_DUPLICATE";
      report.note = "A bread-like item already exists in the exact target list; no chat write was sent to avoid a duplicate.";
      save();
      console.log(JSON.stringify(report));
      return;
    }

    await page.evaluate(async () => {
      const chat = document.querySelector("habab-setup-chat");
      if (!chat?.sessionId || chat.busy) throw new Error("chat_not_ready");
      if (!chat.q("dialog")?.open) chat.q(".launcher")?.click();
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const composer = chat.q("textarea");
      composer.value = "добавь хлеб в список покупок";
      composer.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: composer.value }));
      chat.q("form").requestSubmit();
    });

    await page.waitForFunction(() => {
      const chat = document.querySelector("habab-setup-chat");
      return chat && !chat.busy && chat.messages?.length >= 2 && chat.messages.at(-1)?.role === "assistant";
    }, { timeout: 240000 });

    const chatTurn = await page.evaluate(() => {
      const chat = document.querySelector("habab-setup-chat");
      const latest = chat.messages.at(-1)?.text || "";
      return {
        user_visible_chat_turn_complete: true,
        last_role: chat.messages.at(-1)?.role,
        success_claim: /добавил|добавила|добавлено|готово|добавил(а)?\b/iu.test(latest),
        explicit_failure: /не удалось|ошибка|не добавил|не могу/iu.test(latest),
      };
    });
    report.checks.push({ name: "user_visible_chat", ...chatTurn });

    const afterChat = await getItems();
    report.checks.push({ name: "todo_websocket_readback_after_chat", ...afterChat });

    await page.goto(`${base}/todo?entity_id=${encodeURIComponent(entityId)}`, { waitUntil: "networkidle2", timeout: 90000 });
    await page.waitForFunction((id) => {
      const hass = document.querySelector("home-assistant")?.hass;
      return hass?.user?.is_owner && hass.states?.[id] && location.pathname === "/todo";
    }, { timeout: 60000 }, entityId);
    await page.waitForFunction(() => {
      const panel = document.querySelector("ha-panel-todo");
      const cards = panel?.shadowRoot?.querySelectorAll("hui-todo-list-card");
      return Boolean(cards?.length && [...cards].some((card) => card._items !== undefined));
    }, { timeout: 45000 }).catch(() => {});

    const uiReadback = await page.evaluate((id) => {
      const findAll = (selector, root) => {
        const found = [...root.querySelectorAll(selector)];
        for (const element of root.querySelectorAll("*")) {
          if (element.shadowRoot) found.push(...findAll(selector, element.shadowRoot));
        }
        return found;
      };
      const cards = findAll("hui-todo-list-card", document);
      const todoEntities = Object.keys(document.querySelector("home-assistant")?.hass?.states || {})
        .filter((key) => key.startsWith("todo."));
      const items = cards.flatMap((card) => {
        const selected = card.config?.entity === id ||
          (!card.config?.entity && todoEntities.length === 1 && todoEntities[0] === id);
        return selected && Array.isArray(card._items) ? card._items : [];
      });
      const visibleSummaries = findAll(".summary", document).map((element) => element.textContent.trim());
      return {
        route: location.pathname,
        target_entity_selected: cards.some((card) => card.config?.entity === id) ||
          (cards.length === 1 && todoEntities.length === 1 && todoEntities[0] === id),
        loaded_item_count: items.length,
        displayed_bread_count: visibleSummaries.filter((value) => /хлеб/iu.test(value)).length,
      };
    }, entityId);
    report.checks.push({ name: "todo_panel_after_reload", ...uiReadback });
    const afterReload = await getItems();
    report.checks.push({ name: "todo_websocket_readback_after_ui_reload", ...afterReload });

    report.classification = chatTurn.success_claim && afterChat.bread_count > 0 &&
      afterReload.bread_count > 0 && uiReadback.target_entity_selected &&
      uiReadback.displayed_bread_count > 0 ? "REPRODUCED_PASS" :
      chatTurn.success_claim && afterReload.bread_count === 0 ? "REPRODUCED_FALSE_SUCCESS" : "REPRODUCED_OTHER";
    save();
    console.log(JSON.stringify(report));
  } catch (error) {
    report.classification = "BLOCKED_OR_FAILED";
    report.error = String(error?.message || error).slice(0, 180);
    save();
    console.error(JSON.stringify({ classification: report.classification, error: report.error }));
    process.exitCode = 1;
  } finally {
    if (context) await context.close();
    if (browser) browser.disconnect();
  }
});
