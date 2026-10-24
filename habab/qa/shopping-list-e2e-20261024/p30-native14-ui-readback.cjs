const fs = require("node:fs");
const puppeteer = require("/root/homeassistent/habab/node_modules/puppeteer-core");
const { withOwner } = require("/root/.cache/habab-android/owner-auth-qa.cjs");

const base = "http://127.0.0.1:18124";
const entityId = "todo.shopping_list";
const reportPath = `${__dirname}/p30-native14-ui-readback.json`;
const report = {
  classification: "READ_ONLY_FRESH_TODO_UI_READBACK",
  entity_id: entityId,
  friendly_name_expected: "Список покупок",
  original_chat_request_repeated: false,
  list_mutation_attempted: false,
};

function save() {
  fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
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
        if (panel._entityId !== id || !panel.shadowRoot) return false;
        const cards = [];
        const findCards = (root) => {
          for (const element of root.querySelectorAll("*")) {
            if (element.localName === "hui-todo-list-card") cards.push(element);
            if (element.shadowRoot) findCards(element.shadowRoot);
          }
        };
        findCards(panel.shadowRoot);
        return cards.some((card) => {
          const config = card._config || card.config;
          return (config?.entity === id || config?.actual_entity_id === id) && Array.isArray(card._items);
        });
      });
    }, { timeout: 45000 }, entityId);
    const result = await page.evaluate(async (id) => {
      const hass = document.querySelector("home-assistant")?.hass;
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
      const matchingCards = cards.filter((card) => {
        const config = card._config || card.config;
        return config?.entity === id || config?.actual_entity_id === id;
      });
      const uiItems = matchingCards.flatMap((card) => Array.isArray(card._items) ? card._items : []);
      const state = hass.states[id];
      const response = await hass.callWS({ type: "todo/item/list", entity_id: id });
      const apiItems = Array.isArray(response?.items) ? response.items : [];
      const breadCount = (items) => items.filter((item) => String(item.summary || "").trim().toLocaleLowerCase() === "хлеб").length;
      return {
        core_version: hass.config.version,
        route: location.pathname,
        authenticated_admin: Boolean(hass.user?.is_admin),
        list_friendly_name: state?.attributes?.friendly_name || null,
        panel_entity_id: panel?._entityId || null,
        target_entity_selected: matchingCards.length > 0,
        card_items_loaded: matchingCards.some((card) => Array.isArray(card._items)),
        ui_item_count: uiItems.length,
        ui_bread_count: breadCount(uiItems),
        api_item_count: apiItems.length,
        api_bread_count: breadCount(apiItems),
      };
    }, entityId);
    report.readback = result;
    report.classification = result.core_version === "2026.10.2" && result.authenticated_admin &&
      result.list_friendly_name === "Список покупок" && result.panel_entity_id === entityId &&
      result.target_entity_selected && result.card_items_loaded && result.ui_item_count === result.api_item_count &&
      result.ui_bread_count === result.api_bread_count && result.ui_bread_count === 1
      ? "P30_NATIVE14_PERSISTED_ITEM_VISIBLE_AFTER_FRESH_UI_RELOAD"
      : "P30_NATIVE14_READBACK_MISMATCH";
    save();
    console.log(JSON.stringify({ classification: report.classification, readback: result }));
    await page.close();
  } catch (error) {
    report.classification = "P30_NATIVE14_READBACK_FAILED_NO_MUTATION";
    report.error = `${error.name}: ${String(error.message).slice(0, 120)}`;
    save();
    console.error(JSON.stringify({ classification: report.classification, error: report.error }));
    process.exitCode = 1;
  } finally {
    if (context) await context.close();
    if (browser) browser.disconnect();
  }
}).catch((error) => {
  report.classification = "P30_NATIVE14_READBACK_AUTH_FAILED_NO_MUTATION";
  report.error = `${error.name}: ${String(error.message).slice(0, 120)}`;
  save();
  console.error(JSON.stringify({ classification: report.classification, error: report.error }));
  process.exitCode = 1;
});
