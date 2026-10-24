import { strings } from "./strings.js";
import "./telemetry-panel.js";

const providers = {
  habab: { name: "Habab", model: "gpt-4.1-mini", keyUrl: "https://habab.ru/" },
  openai: { name: "OpenAI", model: "gpt-4.1-mini", keyUrl: "https://platform.openai.com/api-keys" },
  deepseek: { name: "DeepSeek", model: "deepseek-flash", keyUrl: "https://platform.deepseek.com/api_keys" },
  gemini: { name: "Google Gemini", model: "gemini-3.1-flash-lite", keyUrl: "https://aistudio.google.com/api-keys" },
};

class HababAIPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.provider = "openai";
    this.connections = [];
    this.pipelines = [];
    this.onAssistantAvailability = () => this.updateControls();
  }

  set hass(value) {
    this._hass = value;
    const language = value.locale.language.startsWith("ru") ? "ru" : "en";
    if (language !== this.language) {
      this.language = language;
      this.render();
      this.refresh();
    }
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.hass = value;
    const telemetry = this.shadowRoot.querySelector("habab-telemetry-panel");
    if (telemetry) telemetry.hass = value;
  }

  connectedCallback() {
    window.addEventListener("habab-assistant-availability", this.onAssistantAvailability);
    if (this._hass && !this.shadowRoot.firstChild) this.render();
    if (this._hass) { this.updateControls(); this.refresh(); }
  }

  disconnectedCallback() {
    window.removeEventListener("habab-assistant-availability", this.onAssistantAvailability);
    this.busy = false;
    const key = this.shadowRoot.querySelector("#key");
    if (key) key.value = "";
  }

  t(key) { return strings[this.language || "en"][key] || strings[this.language || "en"].unknown; }
  q(selector) { return this.shadowRoot.querySelector(selector); }
  entry() { return this.connections.find((item) => item.provider === this.provider); }
  render() {
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;height:100%;overflow:auto;container-type:inline-size;color:var(--primary-text-color);background:var(--primary-background-color);font:14px/1.55 var(--paper-font-body1_-_font-family,Roboto,system-ui,sans-serif)}
      *{box-sizing:border-box}::selection{background:var(--primary-color);color:var(--text-primary-color,#fff)}
      header{display:flex;align-items:center;gap:var(--ha-space-4,16px);height:64px;padding-inline:var(--ha-space-4,16px);border-block-end:1px solid var(--divider-color);background:var(--card-background-color)}
      h1{font-size:20px;font-weight:500;margin:0}main{max-width:940px;margin:auto;padding:var(--ha-space-8,32px) var(--ha-space-6,24px) 64px}p{margin:0 0 var(--ha-space-4,16px);max-width:72ch}
      .intro{color:var(--secondary-text-color);font-size:16px}.workspace{display:grid;grid-template-columns:200px minmax(0,1fr);gap:var(--ha-space-8,32px);margin-block:var(--ha-space-8,32px)}
      nav{display:flex;flex-direction:column;gap:var(--ha-space-2,8px)}button,input,textarea{font:inherit}button{cursor:pointer;min-height:44px;padding:10px 16px;border:1px solid var(--divider-color);border-radius:8px;color:var(--primary-text-color);background:var(--card-background-color)}
      nav button{text-align:start}nav button[aria-pressed=true]{background:var(--secondary-background-color);border-color:var(--primary-color);color:var(--primary-text-color);font-weight:500}
      button:hover{background:var(--secondary-background-color)}button:active{filter:brightness(.94)}button:disabled{cursor:default;opacity:.55}
      :is(button,input,textarea,a):focus-visible{outline:3px solid var(--ha-color-focus,var(--primary-color));outline-offset:3px}a{color:var(--primary-text-color);text-underline-offset:3px}
      h2{font-size:22px;font-weight:500;margin:0 0 var(--ha-space-2,8px)}h3{font-size:16px;font-weight:500;margin:var(--ha-space-6,24px) 0 var(--ha-space-2,8px)}
      .help,.status{color:var(--secondary-text-color);font-size:13px}.status{margin-block:var(--ha-space-2,8px)}ol{padding-inline-start:22px;margin:8px 0 16px}li{padding-inline-start:4px;margin-block:8px}
      form{margin-block-start:var(--ha-space-6,24px)}label{display:block;font-weight:500;margin:var(--ha-space-4,16px) 0 var(--ha-space-2,8px)}input,textarea{width:100%;min-width:0;padding:12px;border:1px solid var(--secondary-text-color);border-radius:6px;background:var(--card-background-color);color:var(--primary-text-color);caret-color:var(--primary-color)}
      input{height:48px}.key-row{display:flex;gap:8px}.key-row input{flex:1}.key-row button{flex:none}textarea{resize:vertical;min-height:88px}input::placeholder,textarea::placeholder{color:var(--secondary-text-color);opacity:1}
      .actions{display:flex;flex-wrap:wrap;gap:var(--ha-space-2,8px);margin-block:var(--ha-space-4,16px)}.primary{background:var(--ha-color-fill-primary-loud-hover,var(--dark-primary-color));border-color:transparent;color:var(--ha-color-on-primary-loud,var(--text-primary-color))}.primary:hover{filter:brightness(.93);background:var(--ha-color-fill-primary-loud-hover,var(--dark-primary-color))}
      #feedback{padding:var(--ha-space-4,16px);border:1px solid var(--divider-color);border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere}#feedback[data-error=true]{color:var(--error-color)}[hidden]{display:none!important}
      .assistant{padding-block-start:var(--ha-space-8,32px);border-block-start:1px solid var(--divider-color)}.links{display:flex;flex-wrap:wrap;gap:12px 24px;margin-block:16px}.disclosure{margin-block-start:32px}
      @container(max-width:650px){main{padding:24px 16px 48px}.workspace{grid-template-columns:minmax(0,1fr);gap:24px;margin-block:24px}nav{flex-direction:row;flex-wrap:wrap}nav button{flex:1 1 auto;text-align:center}.key-row{flex-wrap:wrap}.key-row input{flex:1 1 100%}h2{font-size:21px}.actions button{flex:1 1 auto}}
    </style>
    <header><ha-menu-button></ha-menu-button><h1>${this.t("title")}</h1></header>
    <main><p class="intro">${this.t("intro")}</p><div class="workspace">
      <nav aria-label="${this.t("provider")}">${Object.entries(providers).map(([id, item]) => `<button type="button" data-provider="${id}" aria-pressed="${id === this.provider}">${item.name}</button>`).join("")}</nav>
      <section aria-labelledby="provider-title"><h2 id="provider-title"></h2><p class="status" id="connection-status"></p>
      <a id="key-link" target="_blank" rel="noopener noreferrer">${this.t("getKey")}</a>
      <h3>${this.t("guide")}</h3><ol id="guide"></ol>
      <form><label for="model">${this.t("model")}</label><input id="model" name="model" required maxlength="128" autocomplete="off" aria-describedby="model-help"><p class="help" id="model-help">${this.t("modelHelp")}</p>
      <label for="key">${this.t("key")}</label><div class="key-row"><input id="key" name="api_key" type="password" required maxlength="512" autocomplete="off" spellcheck="false" autocapitalize="off" aria-describedby="key-help"><button type="button" id="reveal" aria-pressed="false">${this.t("show")}</button></div>
      <p class="help" id="key-help">${this.t("keyHelp")}</p><div class="actions"><button class="primary" type="submit">${this.t("save")}</button><button type="button" id="check">${this.t("check")}</button></div><p class="help">${this.t("cost")}</p></form>
      <div id="feedback" role="status" aria-live="polite" hidden></div></section></div>
      <section class="assistant" aria-labelledby="assistant-title"><h2 id="assistant-title">${this.t("assistantTitle")}</h2><p>${this.t("assistantHelp")}</p>
      <div class="actions"><button type="button" id="open-assistant" class="primary">${this.t("openAssistant")}</button></div><p class="help status" id="assistant-status" role="status" aria-live="polite"></p>
      <div class="links"><a href="/config/voice-assistants/expose">${this.t("expose")}</a><a href="/config/voice-assistants/assistants">${this.t("pipelineSettings")}</a></div>
      <p class="help" id="pipeline-status"></p></section><p class="help disclosure">${this.t("cloud")}</p>
      <habab-telemetry-panel></habab-telemetry-panel></main>`;
    this.q("ha-menu-button").hass = this._hass;
    this.q("habab-telemetry-panel").hass = this._hass;
    this.q("nav").addEventListener("click", (event) => {
      const button = event.target.closest("[data-provider]");
      if (!button || this.busy) return;
      this.provider = button.dataset.provider;
      this.selectProvider();
    });
    this.q("form").addEventListener("submit", (event) => { event.preventDefault(); this.save(); });
    this.q("#reveal").addEventListener("click", () => {
      const showing = this.q("#key").type === "password";
      this.q("#key").type = showing ? "text" : "password";
      this.q("#reveal").textContent = this.t(showing ? "hide" : "show");
      this.q("#reveal").setAttribute("aria-pressed", String(showing));
    });
    this.q("#check").addEventListener("click", () => this.check());
    this.q("#open-assistant").addEventListener("click", () => this.openAssistant());
    this.selectProvider();
  }

  selectProvider() {
    const provider = providers[this.provider];
    this.q("#provider-title").textContent = provider.name;
    this.q("#key-link").href = provider.keyUrl;
    this.q("#key-link").textContent = this.t(this.provider === "habab" ? "hababOpen" : "getKey");
    this.q("#model").value = this.entry()?.model || provider.model;
    this.q("#key").value = "";
    this.q("#key").type = "password";
    this.q("#reveal").textContent = this.t("show");
    this.q("#reveal").setAttribute("aria-pressed", "false");
    this.q("#guide").replaceChildren(...this.t(`${this.provider}Guide`).map((text) => {
      const item = document.createElement("li"); item.textContent = text; return item;
    }));
    this.shadowRoot.querySelectorAll("[data-provider]").forEach((item) => item.setAttribute("aria-pressed", String(item.dataset.provider === this.provider)));
    this.q("#feedback").hidden = true;
    this.updateControls();
  }

  async refresh() {
    try {
      const [connections, pipelines] = await Promise.all([
        this._hass.callWS({ type: "habab_home/providers/list" }),
        this._hass.callWS({ type: "assist_pipeline/pipeline/list" }),
      ]);
      this.connections = connections.connections;
      this.pipelines = pipelines.pipelines;
      this.updateControls();
      this.q("#pipeline-status").textContent = this.pipelines.length
        ? this.t("pipelinesReady").replace("{count}", String(this.pipelines.length))
        : this.t("pipelinesMissing");
    } catch (error) { this.feedback(this.t(error.code), true); }
  }

  updateControls() {
    const entry = this.entry();
    this.shadowRoot.querySelectorAll("button,input,textarea").forEach((item) => { item.disabled = !!this.busy; });
    this.q("#check").disabled = !!this.busy || !entry;
    this.q("#connection-status").textContent = this.t(entry?.assistant_selected ? "savedForAssistant" : entry ? "saved" : "disconnected");
    const openAssistant = this.q("#open-assistant");
    openAssistant.disabled = !!this.busy || window.hababAssistant?.available !== true;
    this.q("#assistant-status").textContent = window.hababAssistant?.available === true
      ? "" : this.t("assistantUnavailable");
  }

  feedback(text, error = false, selector = "#feedback") {
    const el = this.q(selector);
    el.textContent = text;
    el.dataset.error = String(error);
    el.hidden = false;
  }

  async action(fn, timeoutMessage) {
    if (this.busy) return;
    this.busy = true; this.updateControls();
    this.feedback(this.t("checking"));
    try { await fn(); } catch (error) {
      this.feedback(error.code === "timeout" && timeoutMessage ? this.t(timeoutMessage) : this.t(error.code), true);
    }
    finally { this.busy = false; this.updateControls(); }
  }

  async save() {
    if (!this.q("form").reportValidity()) return;
    await this.action(async () => {
      await this._hass.callWS({ type: "habab_home/providers/save", provider: this.provider,
        model: this.q("#model").value.trim(), api_key: this.q("#key").value.trim() });
      this.q("#key").value = "";
      await this.refresh();
      this.feedback(this.t("verified"));
    }, "providerSaveTimeout");
  }

  async check() {
    if (!this.entry()) return;
    await this.action(async () => {
      await this._hass.callWS({ type: "habab_home/providers/check", entry_id: this.entry().entry_id });
      this.feedback(this.t("verified"));
    }, "providerCheckTimeout");
  }

  openAssistant() {
    const opened = window.hababAssistant?.open({}, this.q("#open-assistant")) === true;
    this.q("#assistant-status").textContent = opened ? "" : this.t("assistantUnavailable");
    return opened;
  }
}

if (!customElements.get("habab-ai-panel")) customElements.define("habab-ai-panel", HababAIPanel);
