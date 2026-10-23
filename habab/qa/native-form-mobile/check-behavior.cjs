const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const puppeteer = require("/root/.nvm/versions/node/v22.21.1/lib/node_modules/puppeteer");

const root = path.resolve(__dirname, "../../..");
const sourcePath = path.join(root, "habab/distribution/src/habab_home/frontend/setup-chat.js");
const reportPath = path.join(__dirname, "behavior-report.json");

async function main() {
  const source = fs.readFileSync(sourcePath, "utf8");
  const browser = await puppeteer.connect({browserURL: "http://127.0.0.1:9222"});
  const beforeTargets = new Set(browser.targets().map((target) => target._targetId));
  const context = await browser.createBrowserContext();
  const results = [];
  try {
    const page = await context.newPage();
    await page.setViewport({width: 390, height: 844, deviceScaleFactor: 1, isMobile: false, hasTouch: true});
    await page.setContent(`<!doctype html><html data-habab-demo="true"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><ha-demo><home-assistant-main></home-assistant-main></ha-demo></body></html>`);
    await page.evaluate(() => {
      class HomeAssistantElement extends HTMLElement {}
      customElements.define("home-assistant", HomeAssistantElement);
      const main = document.querySelector("home-assistant-main");
      main.hass = {user: {is_admin: true}, locale: {language: "ru"}};
      const messages = Array.from({length: 18}, (_, index) => ({
        role: index % 2 ? "assistant" : "user",
        content: index % 2
          ? `**Сообщение ${index}**\n\nДлинное сообщение для прокрутки журнала. ${"Текст. ".repeat(12)}`
          : `Запрос ${index}`,
      }));
      window.fetch = async () => ({ok: true, json: async () => ({messages})});
    });
    await page.addScriptTag({content: source, type: "module"});
    await page.waitForFunction(() => {
      const chat = document.querySelector("habab-setup-chat");
      return chat?.shadowRoot?.querySelector("dialog")?.open && chat.sessionId === "public-demo";
    }, {timeout: 5000});
    await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 220)));
    const chat = await page.$("habab-setup-chat");
    const measurements = async () => page.evaluate(() => {
      const chat = document.querySelector("habab-setup-chat");
      const dialog = chat.shadowRoot.querySelector("dialog").getBoundingClientRect();
      const dashboard = document.querySelector("body > ha-demo").getBoundingClientRect();
      return {
        innerHeight: window.innerHeight,
        dashboardHeight: Math.round(dashboard.height),
        chatTop: Math.round(dialog.top),
        chatHeight: Math.round(dialog.height),
        expanded: chat.mobileExpanded,
        inert: document.querySelector("body > ha-demo").inert,
      };
    });
    const expectExpanded = async (label) => {
      const value = await measurements();
      assert.equal(value.dashboardHeight, 0, `${label}: dashboard should collapse`);
      assert.ok(Math.abs(value.chatTop) <= 16, `${label}: chat should fill viewport: ${JSON.stringify(value)}`);
      assert.ok(Math.abs(value.chatHeight - value.innerHeight) <= 12, `${label}: chat should fill viewport height: ${JSON.stringify(value)}`);
      assert.equal(value.expanded, true, `${label}: expanded state`);
      assert.equal(value.inert, true, `${label}: dashboard inert state`);
      results.push({check: label, result: "PASS", ...value});
    };
    const expectCollapsed = async (label) => {
      const value = await measurements();
      const half = value.innerHeight / 2;
      assert.ok(Math.abs(value.dashboardHeight - half) <= 12, `${label}: dashboard should be half viewport: ${JSON.stringify(value)}`);
      assert.ok(Math.abs(value.chatTop - half) <= 12, `${label}: chat should start at half viewport: ${JSON.stringify(value)}`);
      assert.ok(Math.abs(value.chatHeight - half) <= 12, `${label}: chat should be half viewport: ${JSON.stringify(value)}`);
      assert.equal(value.expanded, false, `${label}: collapsed state`);
      assert.equal(value.inert, false, `${label}: dashboard restored`);
      results.push({check: label, result: "PASS", ...value});
    };

    await expectCollapsed("mobile initial split");
    await page.evaluate(() => document.querySelector("habab-setup-chat").shadowRoot.querySelector("textarea").focus());
    await expectExpanded("composer focus expands");
    await page.evaluate(() => document.querySelector("habab-setup-chat").collapseMobileChat());
    await expectCollapsed("collapse control focus stays collapsed");

    await page.evaluate(() => {
      const chat = document.querySelector("habab-setup-chat");
      const container = chat.shadowRoot.querySelector("#setup-cards");
      const nativeFlow = document.createElement("ha-config-flow");
      const flowRoot = nativeFlow.attachShadow({mode: "open"});
      const nativeForm = document.createElement("ha-form");
      const formRoot = nativeForm.attachShadow({mode: "open"});
      const fields = document.createElement("div");
      fields.innerHTML = `
        <input id="editable" aria-label="Editable field">
        <input id="readonly" readonly aria-label="Read only field">
        <input id="disabled" disabled aria-label="Disabled field">
        <textarea id="textarea" aria-label="Text area"></textarea>
        <select id="select" aria-label="Select"><option>one</option></select>
        <div id="editable-content" contenteditable="true" aria-label="Editable text"></div>
        <button id="submit" type="submit">Submit</button>`;
      formRoot.append(fields);
      flowRoot.append(nativeForm);
      container.append(nativeFlow);
      chat.integrationCard = {cards: new Map([["fixture", {form: nativeFlow}]])};
    });

    const focusInForm = async (id) => page.evaluate((fieldId) => {
      document.querySelector("habab-setup-chat").shadowRoot
        .querySelector("ha-config-flow").shadowRoot.querySelector("ha-form")
        .shadowRoot.querySelector(`#${fieldId}`).focus();
    }, id);
    await focusInForm("editable");
    await expectExpanded("native ha-form input focus expands");
    await page.evaluate(() => document.querySelector("habab-setup-chat").collapseMobileChat());
    await focusInForm("textarea");
    await expectExpanded("native ha-form textarea focus expands");
    await page.evaluate(() => document.querySelector("habab-setup-chat").collapseMobileChat());
    await focusInForm("select");
    await expectExpanded("native ha-form select focus expands");
    await page.evaluate(() => document.querySelector("habab-setup-chat").collapseMobileChat());
    await focusInForm("editable-content");
    await expectExpanded("native editable content focus expands");
    await page.evaluate(() => document.querySelector("habab-setup-chat").collapseMobileChat());
    await focusInForm("readonly");
    await expectCollapsed("readonly input focus stays collapsed");
    await focusInForm("submit");
    await expectCollapsed("submit button focus stays collapsed");
    await page.evaluate(() => {
      const field = document.querySelector("habab-setup-chat").shadowRoot
        .querySelector("ha-config-flow").shadowRoot.querySelector("ha-form").shadowRoot.querySelector("#disabled");
      field.focus();
    });
    await expectCollapsed("disabled input cannot expand");

    await page.evaluate(() => {
      const chat = document.querySelector("habab-setup-chat");
      const textarea = chat.shadowRoot.querySelector("textarea");
      textarea.value = "draft survives markdown upgrade";
      textarea.focus();
      const log = chat.shadowRoot.querySelector("#log");
      log.scrollTop = 50;
    });
    const fallbackTag = await page.evaluate(() => document.querySelector("habab-setup-chat").shadowRoot.querySelector("#messages .message.assistant > p")?.tagName);
    assert.equal(fallbackTag, "P", "fixture should begin before ha-markdown registration");
    await page.evaluate(() => {
      window.__markdownCompletions = [];
      class MarkdownFixture extends HTMLElement {
        constructor() {
          super();
          this.updateComplete = new Promise((resolve) => window.__markdownCompletions.push(resolve));
        }
        set content(value) { this._content = value; this.textContent = value; }
        set breaks(value) { this._breaks = value; }
      }
      customElements.define("ha-markdown", MarkdownFixture);
    });
    await page.waitForFunction(() => document.querySelector("habab-setup-chat").shadowRoot.querySelector("#messages ha-markdown"), {timeout: 5000});
    const markdownState = await page.evaluate(() => {
      const chat = document.querySelector("habab-setup-chat");
      return {
        markdownTag: chat.shadowRoot.querySelector("#messages .message.assistant > ha-markdown")?.tagName,
        draft: chat.shadowRoot.querySelector("textarea").value,
        composerFocused: chat.shadowRoot.activeElement === chat.shadowRoot.querySelector("textarea"),
        scrollTop: chat.shadowRoot.querySelector("#log").scrollTop,
      };
    });
    assert.equal(markdownState.markdownTag, "HA-MARKDOWN");
    assert.equal(markdownState.draft, "draft survives markdown upgrade");
    assert.equal(markdownState.composerFocused, true);
    assert.ok(markdownState.scrollTop >= 40 && markdownState.scrollTop <= 60, `scroll position should be preserved, got ${markdownState.scrollTop}`);
    results.push({check: "ha-markdown registration rerenders history and preserves draft, focus, scroll", result: "PASS", ...markdownState});
    const staleCompletion = await page.evaluate(async () => {
      const chat = document.querySelector("habab-setup-chat");
      const log = chat.shadowRoot.querySelector("#log");
      const resolvers = window.__markdownCompletions;
      const staleCount = resolvers.length;
      log.scrollTop = 200;
      const expectedScrollTop = log.scrollTop;
      chat.draw({preserveScroll: true});
      const newerDrawScrollTop = log.scrollTop;
      resolvers.splice(0, staleCount).forEach((resolve) => resolve());
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      return {staleCount, expectedScrollTop, newerDrawScrollTop, afterStaleCompletion: log.scrollTop};
    });
    assert.ok(staleCompletion.staleCount > 0, "fixture should hold pending Markdown completion promises");
    assert.ok(staleCompletion.expectedScrollTop >= 190, JSON.stringify(staleCompletion));
    assert.equal(staleCompletion.newerDrawScrollTop, staleCompletion.expectedScrollTop);
    assert.equal(staleCompletion.afterStaleCompletion, staleCompletion.expectedScrollTop, "stale completion must not overwrite a newer draw's scroll");
    results.push({check: "stale ha-markdown completion cannot overwrite newer draw scroll", result: "PASS", ...staleCompletion});

    await page.setViewport({width: 1280, height: 800, deviceScaleFactor: 1, isMobile: false, hasTouch: true});
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const desktop = await page.evaluate(() => {
      const chat = document.querySelector("habab-setup-chat");
      return {
        viewportWidth: document.documentElement.clientWidth,
        dashboardWidth: Math.round(document.querySelector("body > ha-demo").getBoundingClientRect().width),
        chatWidth: Math.round(chat.shadowRoot.querySelector("dialog").getBoundingClientRect().width),
      };
    });
    assert.ok(Math.abs(desktop.dashboardWidth + desktop.chatWidth - desktop.viewportWidth) <= 32, JSON.stringify(desktop));
    assert.ok(desktop.dashboardWidth >= 600 && desktop.chatWidth >= 600);
    results.push({check: "desktop split geometry remains intact", result: "PASS", ...desktop});

    const report = {
      created_at: new Date().toISOString(),
      fixture: "current setup-chat.js loaded with a mocked public history request and nested ha-config-flow/ha-form shadow roots",
      environment: {viewport_mobile: "390x844", viewport_desktop: "1280x800", physical_android_keyboard: false, live_home_assistant: false, auth_or_form_submission: false},
      checks: results,
    };
    fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  } finally {
    await context.close();
  }
  const afterTargets = new Set(browser.targets().map((target) => target._targetId));
  assert.ok([...beforeTargets].every((targetId) => afterTargets.has(targetId)), "all pre-existing shared Chrome targets must remain open");
  browser.disconnect();
  process.stdout.write(`${JSON.stringify({checks: results.length, report: reportPath, preexisting_shared_targets_preserved: true}, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
