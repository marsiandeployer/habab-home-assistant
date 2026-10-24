const fs = require("node:fs");
const path = require("node:path");
const puppeteer = require("/root/homeassistent/habab/node_modules/puppeteer-core");

const origin = "https://habab-demo.wpmix.net";
const fixtureName = "QA Shopping List E2E 20261024";
const outputPath = path.join(__dirname, "cloud-fixture.json");
const auth = JSON.parse(fs.readFileSync("/root/.local/share/habab-owner-test/qa-current-auth.json", "utf8"));
const report = {
  target: `${origin}/owner-test → /config/integrations/dashboard`,
  runtime: "habab-owner-test; habab-home:owner-integrated-qa-0.3.4-20261024-v15",
  source: "a3c1d3f06",
  image_sha256: "47c06194a233b0927f31e31176b332c783636717d82d786bb7b5311ccf762c59",
  fixture_name: fixtureName,
  integration_domain: "local_todo",
};

function save() {
  fs.writeFileSync(outputPath, `${JSON.stringify(report, null, 2)}\n`);
}

function findAll(selector, root) {
  const found = [...root.querySelectorAll(selector)];
  for (const element of root.querySelectorAll("*")) {
    if (element.shadowRoot) found.push(...findAll(selector, element.shadowRoot));
  }
  return found;
}

(async () => {
  const browser = await puppeteer.connect({ browserURL: "http://127.0.0.1:9222", defaultViewport: null });
  const context = await browser.createBrowserContext();
  let flowOpened = false;
  try {
    const page = await context.newPage();
    await page.setViewport({ width: 1280, height: 800 });
    await page.evaluateOnNewDocument((base, tokens) => {
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
    await page.goto(`${origin}/owner-test`, { waitUntil: "networkidle2", timeout: 60000 });
    await page.goto(`${origin}/config/integrations/dashboard`, { waitUntil: "networkidle2", timeout: 60000 });
    await page.waitForFunction(() => document.querySelector("home-assistant")?.hass?.user?.is_admin, { timeout: 60000 });

    const before = await page.evaluate(async (title) => {
      const hass = document.querySelector("home-assistant").hass;
      const entries = await hass.callWS({ type: "config_entries/get" });
      const registry = await hass.callWS({ type: "config/entity_registry/list" });
      const states = await hass.callWS({ type: "get_states" });
      return {
        exact_fixture_exists: entries.some((entry) => entry.domain === "local_todo" && entry.title === title),
        todo_entities: registry.filter((entry) => entry.entity_id.startsWith("todo.")).map((entry) => entry.entity_id),
        todo_state_count: states.filter((state) => state.entity_id.startsWith("todo.")).length,
      };
    }, fixtureName);
    report.readbefore = before;
    if (before.exact_fixture_exists) {
      report.classification = "FIXTURE_ALREADY_EXISTS_NO_DUPLICATE";
      save();
      console.log(JSON.stringify(report));
      return;
    }
    if (before.todo_entities.length || before.todo_state_count) {
      report.classification = "ABORT_EXISTING_LISTS_NEED_TARGET_REVIEW";
      save();
      console.log(JSON.stringify(report));
      return;
    }

    await page.evaluate(() => {
      const add = window.__qaFindAll("ha-button", document).find((button) =>
        button.textContent.trim() === "Добавить интеграцию");
      if (!add) throw new Error("add_integration_button_not_found");
      add.click();
    });
    await page.waitForFunction(() => window.__qaFindAll("dialog-add-integration", document).some((dialog) => dialog._open), { timeout: 30000 });
    const searchHandle = await page.evaluateHandle(() => {
      const dialog = window.__qaFindAll("dialog-add-integration", document)[0];
      const search = dialog?.shadowRoot?.querySelector("ha-input-search");
      return search?.shadowRoot?.querySelector("wa-input")?.input;
    });
    const searchInput = searchHandle.asElement();
    if (!searchInput) throw new Error("integration_search_input_not_found");
    await searchInput.click();
    await searchInput.type("Local To-do", { delay: 20 });
    await page.waitForFunction(() => window.__qaFindAll("ha-integration-list-item", document).some((item) =>
      item.integration?.domain === "local_todo" && item.getClientRects().length), { timeout: 30000 });
    const selected = await page.evaluate(() => {
      const item = window.__qaFindAll("ha-integration-list-item", document).find((row) =>
        row.integration?.domain === "local_todo" && row.getClientRects().length);
      if (!item) return false;
      item.click();
      return true;
    });
    searchHandle.dispose();
    if (!selected) throw new Error("local_todo_result_not_found");

    await page.waitForFunction(() => window.__qaFindAll("dialog-data-entry-flow", document).some((dialog) => dialog._step?.type === "form"), { timeout: 45000 });
    flowOpened = true;
    const schema = await page.evaluate(() => {
      const dialog = window.__qaFindAll("dialog-data-entry-flow", document)[0];
      return {
        domain: dialog._params?.domain || dialog._step?.handler,
        stepType: dialog._step?.type,
        stepId: dialog._step?.step_id,
        fields: (dialog._step?.data_schema || []).map((field) => ({ name: field.name, required: !!field.required })),
      };
    });
    report.flow_form = schema;
    if (schema.domain !== "local_todo" || schema.stepType !== "form" ||
        schema.fields.length !== 1 || schema.fields[0].name !== "todo_list_name" || !schema.fields[0].required) {
      await page.evaluate(() => window.__qaFindAll("dialog-data-entry-flow", document)[0]?.closeDialog());
      flowOpened = false;
      report.classification = "ABORT_UNEXPECTED_LOCAL_TODO_FORM";
      save();
      console.log(JSON.stringify(report));
      return;
    }

    await page.waitForFunction(() => {
      const field = window.__qaFindAll("ha-form-string", document)[0];
      return customElements.get("ha-form-string") && field?.shadowRoot;
    }, { timeout: 20000 });

    const inputHandle = await page.evaluateHandle(async () => {
      const dialog = window.__qaFindAll("dialog-data-entry-flow", document)[0];
      const form = dialog.shadowRoot.querySelector("step-flow-form");
      const field = form && window.__qaFindAll("ha-form-string", form.shadowRoot)[0];
      await field?.updateComplete;
      const input = field?.shadowRoot?.querySelector("ha-input");
      await input?.updateComplete;
      const webAwesomeInput = input?.shadowRoot?.querySelector("wa-input");
      await webAwesomeInput?.updateComplete;
      return webAwesomeInput?.input;
    });
    const nameInput = inputHandle.asElement();
    if (!nameInput) throw new Error("local_todo_name_input_not_found");
    await nameInput.click();
    await nameInput.type(fixtureName, { delay: 10 });
    const submit = await page.evaluateHandle(() => {
      const dialog = window.__qaFindAll("dialog-data-entry-flow", document)[0];
      const buttons = window.__qaFindAll("ha-button", dialog.shadowRoot);
      return buttons.find((button) => button.slot === "primaryAction");
    });
    if (!submit.asElement()) throw new Error("local_todo_submit_button_not_found");
    await submit.asElement().click();
    await page.waitForFunction(() => window.__qaFindAll("dialog-data-entry-flow", document).some((dialog) => dialog._step?.type === "create_entry"), { timeout: 45000 });
    const finish = await page.evaluateHandle(() => {
      const dialog = window.__qaFindAll("dialog-data-entry-flow", document)[0];
      const buttons = window.__qaFindAll("ha-button", dialog.shadowRoot);
      return buttons.find((button) => button.slot === "primaryAction");
    });
    if (!finish.asElement()) throw new Error("local_todo_finish_button_not_found");
    await finish.asElement().click();
    flowOpened = false;

    await page.waitForFunction(async (title) => {
      const hass = document.querySelector("home-assistant")?.hass;
      if (!hass?.user?.is_admin) return false;
      const entries = await hass.callWS({ type: "config_entries/get" });
      return entries.some((entry) => entry.domain === "local_todo" && entry.title === title && entry.state === "loaded");
    }, { timeout: 45000 }, fixtureName);
    const result = await page.evaluate(async (title) => {
      const hass = document.querySelector("home-assistant").hass;
      const entries = await hass.callWS({ type: "config_entries/get" });
      const entry = entries.find((row) => row.domain === "local_todo" && row.title === title);
      const registry = await hass.callWS({ type: "config/entity_registry/list" });
      const states = await hass.callWS({ type: "get_states" });
      const entities = registry.filter((row) => row.config_entry_id === entry?.entry_id).map((row) => ({
        entity_id: row.entity_id,
        state: states.find((state) => state.entity_id === row.entity_id)?.state || null,
      }));
      return { entry_state: entry?.state, entities };
    }, fixtureName);
    report.result = result;
    report.classification = result.entry_state === "loaded" && result.entities.length === 1 &&
      result.entities[0].entity_id.startsWith("todo.") && result.entities[0].state === "0"
      ? "FIXTURE_CREATED_READBACK_PASS" : "FIXTURE_CREATED_READBACK_FAILED";
    save();
    console.log(JSON.stringify(report));
  } catch (error) {
    report.classification = "FIXTURE_SETUP_FAILED";
    report.error = `${error.name}: ${String(error.message).slice(0, 160)}`;
    save();
    console.error(JSON.stringify({ classification: report.classification, error: report.error }));
    process.exitCode = 1;
  } finally {
    if (flowOpened) {
      await pageCleanup(context).catch(() => {});
    }
    await context.close();
    browser.disconnect();
  }
})().catch((error) => {
  report.classification = "FIXTURE_SETUP_FAILED";
  report.error = `${error.name}: ${String(error.message).slice(0, 160)}`;
  save();
  console.error(JSON.stringify({ classification: report.classification, error: report.error }));
  process.exitCode = 1;
});

async function pageCleanup(context) {
  for (const page of await context.pages()) {
    await page.evaluate(() => window.__qaFindAll("dialog-data-entry-flow", document)[0]?.closeDialog()).catch(() => {});
  }
}
