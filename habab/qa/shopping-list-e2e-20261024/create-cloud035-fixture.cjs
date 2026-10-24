const fs = require("node:fs");
const puppeteer = require("/root/homeassistent/habab/node_modules/puppeteer-core");

const origin = "https://habab-demo.wpmix.net";
const fixtureName = "QA Shopping List E2E 20261024 v0.3.5";
const outputPath = `${__dirname}/cloud035-fixture.json`;
const auth = JSON.parse(fs.readFileSync("/root/.local/share/habab-owner-test/qa-current-auth.json", "utf8"));
const report = {
  target: `${origin}/owner-test → /config/integrations/dashboard`,
  image: "habab-home:owner-gemini-0.3.5-96551e39e",
  source: "96551e39e5ea4b8c3de48e4a47f38d7c330907b8",
  image_sha256: "b4a39badcf4cba07073a989ad6f03650eaa33c85c4bda349ab05c7aa17637987",
  fixture_name: fixtureName,
  integration_domain: "local_todo",
};

function findAll(selector, root) {
  const found = [...root.querySelectorAll(selector)];
  for (const element of root.querySelectorAll("*")) {
    if (element.shadowRoot) found.push(...findAll(selector, element.shadowRoot));
  }
  return found;
}

function save() {
  fs.writeFileSync(outputPath, `${JSON.stringify(report, null, 2)}\n`);
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

    report.readbefore = await page.evaluate(async (title) => {
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
    if (report.readbefore.exact_fixture_exists) {
      report.classification = "FIXTURE_ALREADY_EXISTS_NO_DUPLICATE";
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
      return dialog?.shadowRoot?.querySelector("ha-input-search")?.shadowRoot?.querySelector("wa-input")?.input;
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
      item?.click();
      return Boolean(item);
    });
    searchHandle.dispose();
    if (!selected) throw new Error("local_todo_result_not_found");

    await page.waitForFunction(() => window.__qaFindAll("dialog-data-entry-flow", document).some((dialog) => dialog._step?.type === "form"), { timeout: 45000 });
    flowOpened = true;
    report.flow_form = await page.evaluate(() => {
      const dialog = window.__qaFindAll("dialog-data-entry-flow", document)[0];
      return {
        domain: dialog._params?.domain || dialog._step?.handler,
        step_type: dialog._step?.type,
        step_id: dialog._step?.step_id,
        fields: (dialog._step?.data_schema || []).map((field) => ({ name: field.name, required: !!field.required })),
      };
    });
    if (report.flow_form.domain !== "local_todo" || report.flow_form.step_type !== "form" ||
        report.flow_form.fields.length !== 1 || report.flow_form.fields[0].name !== "todo_list_name" ||
        !report.flow_form.fields[0].required) {
      throw new Error("unexpected_local_todo_form");
    }

    await page.waitForFunction(() => window.__qaFindAll("ha-form-string", document).some((field) => field.shadowRoot), { timeout: 20000 });
    const inputHandle = await page.evaluateHandle(async () => {
      const dialog = window.__qaFindAll("dialog-data-entry-flow", document)[0];
      const form = dialog.shadowRoot.querySelector("step-flow-form");
      const field = form && window.__qaFindAll("ha-form-string", form.shadowRoot)[0];
      await field?.updateComplete;
      const input = field?.shadowRoot?.querySelector("ha-input");
      await input?.updateComplete;
      const webInput = input?.shadowRoot?.querySelector("wa-input");
      await webInput?.updateComplete;
      return webInput?.input;
    });
    const nameInput = inputHandle.asElement();
    if (!nameInput) throw new Error("local_todo_name_input_not_found");
    await nameInput.click();
    await nameInput.type(fixtureName, { delay: 10 });
    const submit = await page.evaluateHandle(() => {
      const dialog = window.__qaFindAll("dialog-data-entry-flow", document)[0];
      return window.__qaFindAll("ha-button", dialog.shadowRoot).find((button) => button.slot === "primaryAction");
    });
    if (!submit.asElement()) throw new Error("local_todo_submit_button_not_found");
    await submit.asElement().click();
    await page.waitForFunction(() => window.__qaFindAll("dialog-data-entry-flow", document).some((dialog) => dialog._step?.type === "create_entry"), { timeout: 45000 });
    const finish = await page.evaluateHandle(() => {
      const dialog = window.__qaFindAll("dialog-data-entry-flow", document)[0];
      return window.__qaFindAll("ha-button", dialog.shadowRoot).find((button) => button.slot === "primaryAction");
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
      return {
        entry_state: entry?.state,
        entities: registry.filter((row) => row.config_entry_id === entry?.entry_id).map((row) => ({
          entity_id: row.entity_id,
          state: states.find((state) => state.entity_id === row.entity_id)?.state || null,
        })),
      };
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
      for (const page of await context.pages()) {
        await page.evaluate(() => window.__qaFindAll?.("dialog-data-entry-flow", document)[0]?.closeDialog()).catch(() => {});
      }
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
