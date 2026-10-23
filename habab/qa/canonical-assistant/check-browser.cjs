/* Run against a disposable HA owner, never a personal installation. */
const fs = require("node:fs");
const assert = require("node:assert/strict");
const path = require("node:path");
const puppeteer = require("../../node_modules/puppeteer-core");

const privateRoot = process.env.HABAB_QA_PRIVATE;
if (!privateRoot) throw new Error("Set HABAB_QA_PRIVATE to the isolated QA credential directory");
const auth = JSON.parse(fs.readFileSync(path.join(privateRoot, "tokens.json")));
const base = auth.base_url;
if (base !== "http://127.0.0.1:8403") throw new Error("Only the disposable canonical QA target is allowed");
const output = __dirname;
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
  const renewed = await fetch(base + "/auth/token", {
    method: "POST", body: new URLSearchParams({
      grant_type: "refresh_token", refresh_token: auth.refresh_token, client_id: auth.client_id,
    }),
  });
  if (!renewed.ok) throw new Error("Isolated QA credential refresh failed: " + renewed.status);
  Object.assign(auth, await renewed.json());
  fs.writeFileSync(path.join(privateRoot, "tokens.json"), JSON.stringify(auth), {mode: 0o600});
  const profile = fs.mkdtempSync("/tmp/codex-chrome-run-");
  const browser = await puppeteer.launch({
    executablePath: "/snap/bin/chromium", headless: true, userDataDir: profile,
    defaultViewport: {width: 1280, height: 800},
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
  });
  const report = {target: "isolated_qa", cases: [], pageErrors: []};
  let page;
  try {
    page = await browser.newPage();
    page.on("pageerror", (error) => report.pageErrors.push({name: error.name, message: error.message.slice(0, 240), stage: report.cases.at(-1) || "bootstrap"}));
    await page.evaluateOnNewDocument((tokens) => {
      window.__qaUnhandled = [];
      window.addEventListener("unhandledrejection", (event) => {
        const value = event.reason;
        window.__qaUnhandled.push({type: "rejection", code: value?.code || value?.error?.code,
          message: value?.message || value?.error?.message, stack: value?.stack});
      });
      window.addEventListener("error", (event) => {
        window.__qaUnhandled.push({type: "error", message: event.message,
          code: event.error?.code, stack: event.error?.stack});
      });
      localStorage.setItem("hassTokens", JSON.stringify({
        ...tokens, hassUrl: tokens.base_url, clientId: tokens.client_id,
        expires: Date.now() + tokens.expires_in * 1000,
      }));
      localStorage.setItem("selectedLanguage", JSON.stringify("ru"));
      window.deepAll = (selector, root = document) => {
        const result = [];
        for (const element of root.querySelectorAll("*")) {
          if (element.matches(selector)) result.push(element);
          if (element.shadowRoot) result.push(...window.deepAll(selector, element.shadowRoot));
        }
        return result;
      };
    }, auth);
    const navigate = async (route, selector) => {
      await page.evaluate((url) => {
        history.pushState(null, "", url);
        window.dispatchEvent(new CustomEvent("location-changed", {detail: {replace: false}}));
      }, route);
      await page.waitForFunction((sel) => window.deepAll(sel).length > 0, {timeout: 30000}, selector);
    };
    await page.goto(base + "/home/overview", {waitUntil: "networkidle2", timeout: 60000});
    await page.waitForFunction(() => document.querySelector("habab-setup-chat")?.hass?.states && window.hababAssistant?.available, {timeout: 45000});
    assert.equal(await page.evaluate(() => window.hababNativeUi?.version), 1);
    const initial = await page.evaluate(() => {
      const chats = document.querySelectorAll("habab-setup-chat");
      const button = chats[0].q(".launcher").getBoundingClientRect();
      return {count: chats.length, x: button.x, y: button.y, language: chats[0].language};
    });
    assert.equal(initial.count, 1); assert.equal(initial.language, "ru");
    assert(initial.x >= 1200 && initial.y >= 720);
    report.cases.push("one_russian_launcher_bottom_right");

    await page.evaluate(() => document.querySelector("habab-setup-chat").q(".launcher").click());
    await page.waitForFunction(() => {
      const c = document.querySelector("habab-setup-chat");
      return c.sessionId && !c.busy;
    }, {timeout: 60000});
    assert.equal(await page.evaluate(() => document.querySelector("habab-setup-chat").q("dialog").matches(":modal")), false);
    await page.evaluate(() => { document.querySelector("habab-setup-chat").q("textarea").value = "Черновик настройки"; });
    await navigate("/habab-ai", "habab-ai-panel");
    assert.equal(await page.evaluate(() => window.deepAll("habab-ai-panel")[0].shadowRoot.querySelectorAll("textarea").length), 0);
    assert.equal(await page.evaluate(() => document.querySelector("habab-setup-chat").q("textarea").value), "Черновик настройки");
    await page.evaluate(() => document.querySelector("habab-setup-chat").q("dialog").close());
    await page.evaluate(() => window.deepAll("habab-ai-panel")[0].shadowRoot.querySelector("#open-assistant").click());
    assert.equal(await page.evaluate(() => document.querySelectorAll("habab-setup-chat").length), 1);
    report.cases.push("ai_settings_open_same_chat_without_duplicate_composer");
    await page.evaluate(() => document.querySelector("habab-setup-chat").q("dialog").close());
    await navigate("/home/overview", "home-assistant-main");
    await page.keyboard.press("a");
    await page.waitForFunction(() => document.querySelector("habab-setup-chat").q("dialog").open, {timeout: 10000});
    assert.equal(await page.evaluate(() => window.deepAll("ha-voice-command-dialog").length), 0);
    report.cases.push("native_assist_keyboard_routes_to_canonical_chat");
    await page.evaluate(() => { document.querySelector("habab-setup-chat").q("textarea").value = ""; });

    // Real model turn. It must create an actual native config flow, not a demo.
    const input = await page.evaluateHandle(() => document.querySelector("habab-setup-chat").q("textarea"));
    await input.type("Добавь прямо здесь интеграцию Time & Date (time_date). Форму заполню сам. Ничего не отправляй за меня.");
    await input.press("Enter");
    await page.waitForFunction(() => {
      const c = document.querySelector("habab-setup-chat");
      return !c.busy && c.integrationCard?.interactions.some((item) => item.domain === "time_date" && item.phase === "form");
    }, {timeout: 210000});
    await page.waitForFunction(() => window.deepAll("ha-selector-select").length > 0, {timeout: 45000});
    await pause(400);
    await page.evaluate(() => {
      const setup = document.querySelector("habab-setup-chat").integrationCard;
      const item = setup.interactions.find((record) => record.domain === "time_date" && record.phase === "form");
      setup.cards.get(item.id).element.scrollIntoView({block: "end"});
    });
    await page.screenshot({path: path.join(output, "native-form-desktop.png")});
    report.cases.push("real_deepseek_creates_native_form_inside_chat");

    // Native select interaction: the option must be chosen through the UI.
    const select = await page.evaluateHandle(() => window.deepAll("ha-selector-select").find((element) => element.selector?.select?.translation_key === "display_options"));
    assert(select.asElement());
    const dropdown = await page.evaluateHandle((element) => window.deepAll("ha-select", element.shadowRoot)[0], select);
    await dropdown.asElement().click();
    await page.waitForFunction(() => window.deepAll("ha-dropdown-item").some((element) => element.value === "time"), {timeout: 10000});
    const option = await page.evaluateHandle(() => window.deepAll("ha-dropdown-item").find((element) => element.value === "time"));
    await option.asElement().click();
    await pause(200);
    const cardInfo = await page.evaluate(() => {
      const c = document.querySelector("habab-setup-chat");
      const item = c.integrationCard.interactions.find((record) => record.domain === "time_date" && record.phase === "form");
      return {id: item.id, domain: item.domain};
    });
    // The card's submit button delegates to the original HA StepFlowForm.
    const submit = await page.evaluateHandle((id) => {
      const card = document.querySelector("habab-setup-chat").integrationCard.cards.get(id);
      return [...card.element.querySelectorAll("button")].find((button) => button.classList.contains("primary"));
    }, cardInfo.id);
    await submit.asElement().click();
    await page.waitForFunction(() => document.querySelector("habab-setup-chat").integrationCard.interactions.some((item) => item.domain === "time_date" && item.phase === "done" && item.entry_state === "loaded" && item.entity_count > 0), {timeout: 45000});
    // Config-entry readiness can precede delivery of the first state event.
    await page.waitForFunction(() => /^\d{2}:\d{2}$/.test(document.querySelector("habab-setup-chat").hass.states["sensor.time"]?.state || ""), {timeout: 15000});
    report.cases.push("native_selector_submit_creates_loaded_real_time_sensor");
    await page.screenshot({path: path.join(output, "native-connected-desktop.png")});

    // A synthetic form fixture checks selector context compatibility only.
    // It is never submitted and is not evidence of physical device support.
    await page.evaluate(async () => {
      const hass = document.querySelector("habab-setup-chat").hass;
      const step = {
        type: "form", flow_id: "qa-selector-fixture", handler: "time_date", step_id: "user",
        data_schema: ["date", "time", "entity", "area", "icon", "template", "action"].map((name) => ({name, selector: {[name]: {}}})),
        errors: {},
      };
      const loaded = await window.hababNativeUi.loadFlowForm({
        hass, flowType: "config_flow", handler: "time_date", domain: "time_date", step,
        submit: async () => { throw new Error("The selector fixture must not be submitted"); },
      });
      const host = document.createElement(loaded.contextTag);
      host.id = "habab-qa-selector-matrix"; host.hass = hass; host.narrow = true;
      host.style.cssText = "position:fixed;left:16px;top:80px;width:400px;max-height:600px;overflow:auto;background:var(--card-background-color,white)";
      const form = document.createElement(loaded.tag);
      form.hass = hass; form.narrow = true; form.domain = "time_date";
      form.step = step; form.flowConfig = loaded.flowConfig;
      host.append(form); document.body.append(host);
    });
    await page.waitForFunction(() => {
      const root = document.getElementById("habab-qa-selector-matrix");
      return ["date", "time", "entity", "area", "icon", "template", "action"].every((name) =>
        window.deepAll("ha-selector-" + name, root).some((element) => element.shadowRoot?.childNodes.length));
    }, {timeout: 45000});
    await pause(1500);
    await page.evaluate(() => document.getElementById("habab-qa-selector-matrix").remove());
    report.cases.push("synthetic_selector_context_matrix_without_submission");

    await page.evaluate(() => document.querySelector("habab-setup-chat").q("dialog").close());
    await page.setViewport({width: 390, height: 844});
    await page.evaluate(() => document.querySelector("habab-setup-chat").q(".launcher").click());
    assert.equal(await page.evaluate(() => document.querySelector("habab-setup-chat").q("dialog").matches(":modal")), true);
    await pause(300); // Capture the settled dialog, after its 160 ms entrance.
    await page.screenshot({path: path.join(output, "native-connected-mobile.png")});
    await page.evaluate(() => document.querySelector("habab-setup-chat").q("dialog").close());
    const outside = await page.evaluate(() => {
      const hit = document.elementFromPoint(120, 140);
      return hit && hit.localName !== "habab-setup-chat";
    });
    assert(outside);
    report.cases.push("mobile_modal_and_closed_chat_does_not_block_page");
    assert.deepEqual(report.pageErrors, []);
    report.status = "PASS";
  } catch (error) {
    report.status = "FAIL"; report.error = error.message;
    if (page) await page.screenshot({path: path.join(privateRoot, "browser-failed.png")});
    process.exitCode = 1;
  } finally {
    if (page) {
      const details = await page.evaluate(() => window.__qaUnhandled || []).catch(() => []);
      fs.writeFileSync(path.join(privateRoot, "browser-unhandled.json"), JSON.stringify(details, null, 2), {mode: 0o600});
    }
    await browser.close();
    fs.rmSync(profile, {recursive: true, force: true});
    fs.rmSync("/tmp/snap-private-tmp/snap.chromium" + profile, {recursive: true, force: true});
    fs.writeFileSync(path.join(output, "browser-result.json"), JSON.stringify(report, null, 2) + "\n");
    console.log(JSON.stringify(report));
  }
})().catch((error) => { console.error(error.message); process.exitCode = 1; });
