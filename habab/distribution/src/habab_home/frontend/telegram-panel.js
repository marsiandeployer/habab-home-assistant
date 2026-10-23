import { strings } from "./strings.js";

class HababTelegramPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.state = { phase: "not_configured" };
    this.busy = false;
  }

  set hass(value) {
    const firstHass = !this._hass;
    this._hass = value;
    const language = value?.locale?.language?.startsWith("ru") ? "ru" : "en";
    if (language !== this.language) {
      this.language = language;
      this.render();
    }
    const menu = this.q("ha-menu-button");
    if (menu) menu.hass = value;
    if (firstHass && this.shadowRoot.firstChild) this.refresh();
  }

  connectedCallback() {
    if (!this.shadowRoot.firstChild) this.render();
    if (this._hass) this.refresh();
    this.scheduleStatusPoll();
  }

  disconnectedCallback() {
    clearTimeout(this.statusTimer);
    const input = this.q("#token");
    if (input) input.value = "";
    const proxy = this.q("#proxy");
    if (proxy) proxy.value = "";
  }

  t(key) {
    const copy = strings[this.language || "en"];
    return copy[key] || copy.unknown;
  }

  q(selector) { return this.shadowRoot.querySelector(selector); }

  render() {
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;height:100%;overflow:auto;color:var(--primary-text-color);background:var(--primary-background-color);font:14px/1.55 var(--paper-font-body1_-_font-family,Roboto,system-ui,sans-serif)}
      *{box-sizing:border-box}header{display:flex;align-items:center;gap:16px;height:64px;padding-inline:16px;border-block-end:1px solid var(--divider-color);background:var(--card-background-color)}
      h1{font-size:20px;font-weight:500;margin:0}main{max-width:760px;margin:auto;padding:32px 24px 64px}p{max-width:72ch;margin:0 0 16px}.intro{color:var(--secondary-text-color);font-size:16px}
      h2{font-size:22px;font-weight:500;margin:28px 0 8px}.help{color:var(--secondary-text-color);font-size:13px}label{display:block;font-weight:500;margin:24px 0 8px}
      input{width:100%;height:48px;padding:12px;border:1px solid var(--secondary-text-color);border-radius:6px;background:var(--card-background-color);color:var(--primary-text-color);font:inherit;caret-color:var(--primary-color)}
      input:focus-visible,button:focus-visible,a:focus-visible{outline:3px solid var(--ha-color-focus,var(--primary-color));outline-offset:3px}
      button{font:inherit;cursor:pointer;min-height:44px;padding:10px 16px;border:1px solid var(--divider-color);border-radius:8px;color:var(--primary-text-color);background:var(--card-background-color)}
      button:hover{background:var(--secondary-background-color)}button:disabled{cursor:default;opacity:.55}.primary{background:var(--ha-color-fill-primary-loud-hover,var(--dark-primary-color));border-color:transparent;color:var(--ha-color-on-primary-loud,var(--text-primary-color))}
      .actions{display:flex;flex-wrap:wrap;gap:8px;margin-block:16px}.status{padding:12px 0;overflow-wrap:anywhere}.status[data-error=true]{color:var(--error-color)}a{color:var(--primary-color);text-underline-offset:3px}[hidden]{display:none!important}
      @media(max-width:600px){main{padding:24px 16px 48px}.actions button{flex:1 1 auto}}
    </style>
    <header><ha-menu-button></ha-menu-button><h1>${this.t("telegramTitle")}</h1></header>
    <main><p class="intro">${this.t("telegramIntro")}</p><p class="help" id="telegram-security">${this.t("telegramSecurity")}</p>
      <form><label for="token">${this.t("telegramToken")}</label><input id="token" name="token" type="password" required maxlength="512" autocomplete="off" spellcheck="false" autocapitalize="off" aria-describedby="telegram-security">
        <label for="proxy">${this.t("telegramProxy")}</label><input id="proxy" name="proxy" type="password" maxlength="2048" autocomplete="off" spellcheck="false" autocapitalize="off" aria-describedby="telegram-proxy-help"><p class="help" id="telegram-proxy-help">${this.t("telegramProxyHelp")}</p>
        <div class="actions"><button class="primary" type="submit">${this.t("telegramConnect")}</button><button type="button" id="refresh">${this.t("telegramRefresh")}</button><button type="button" id="unlink">${this.t("telegramUnlink")}</button></div>
      </form>
      <h2>${this.t("telegramTitle")}</h2><p class="status" id="status" role="status" aria-live="polite"></p><p id="username" hidden></p><p><a id="pairing" target="_blank" rel="noopener noreferrer" hidden>${this.t("telegramOpenPairing")}</a></p>
      <button type="button" id="pair" hidden>${this.t("telegramPair")}</button>
    </main>`;
    this.q("ha-menu-button").hass = this._hass;
    this.q("form").addEventListener("submit", (event) => { event.preventDefault(); this.configure(); });
    this.q("#refresh").addEventListener("click", () => this.refresh());
    this.q("#pair").addEventListener("click", () => this.pair());
    this.q("#unlink").addEventListener("click", () => this.unlink());
    this.applyState();
  }

  applyState() {
    if (!this.shadowRoot.firstChild) return;
    const key = { not_configured: "telegramNotConfigured", pairing: "telegramPairing", connected: "telegramConnected", error: "telegramError" }[this.state.phase] || "telegramError";
    const errorCopy = {
      invalid_token: "telegramInvalidToken",
      invalid_proxy: "telegramInvalidProxy",
      cannot_validate: "telegramCannotValidate",
      setup_failed: "telegramWorkerFailed",
      worker_error: "telegramWorkerFailed",
      worker_stopped: "telegramWorkerFailed",
      worker_protocol_error: "telegramWorkerFailed",
      not_configured: "telegramNotConfiguredError",
      pairing_url: "telegramPairUnavailable",
    };
    this.q("#status").textContent = this.state.phase === "error"
      ? this.t(errorCopy[this.state.error] || "telegramUnknownError")
      : this.t(key);
    this.q("#status").dataset.error = String(this.state.phase === "error");
    const username = this.q("#username");
    username.hidden = !this.state.bot_username;
    username.textContent = this.state.bot_username ? `${this.t("telegramUsername")}: @${this.state.bot_username.replace(/^@/, "")}` : "";
    const pairing = this.q("#pairing");
    pairing.hidden = !this.state.pairing_url;
    pairing.href = this.state.pairing_url || "#";
    this.q("#pair").hidden = this.state.phase !== "connected";
    this.q("#unlink").hidden = this.state.phase === "not_configured";
    this.updateControls();
    this.scheduleStatusPoll();
  }

  updateControls() {
    if (!this.shadowRoot.firstChild) return;
    this.shadowRoot.querySelectorAll("button,input").forEach((element) => { element.disabled = this.busy; });
    this.q("#pair").hidden = this.busy || this.state.phase !== "connected";
  }

  async request(message) { return this._hass.callWS({ type: `habab_telegram/${message}` }); }

  async refresh() {
    if (!this._hass || this.busy || this.refreshing) return;
    this.refreshing = true;
    try {
      const state = await this.request("status");
      if (!["not_configured", "pairing", "connected", "error"].includes(state.phase)) throw new Error("status");
      this.state = state;
      this.applyState();
    } catch (_) {
      this.state = { phase: "error", error: "unknown" };
      this.applyState();
    } finally { this.refreshing = false; }
  }

  scheduleStatusPoll() {
    clearTimeout(this.statusTimer);
    if (this.isConnected && this.state.phase === "pairing") {
      this.statusTimer = setTimeout(() => this.refresh(), 2500);
    }
  }

  async run(action, successText) {
    if (!this._hass || this.busy) return;
    this.busy = true; this.updateControls();
    try {
      const result = await action();
      if (result?.phase) this.state = result;
      else {
        const state = await this.request("status");
        if (!["not_configured", "pairing", "connected", "error"].includes(state.phase)) throw new Error("status");
        this.state = state;
      }
      if (successText) { this.q("#status").textContent = this.t(successText); this.q("#status").dataset.error = "false"; }
    } catch (error) {
      this.state = { phase: "error", error: error?.code || error?.message || "unknown" };
      this.applyState();
    } finally { this.busy = false; this.applyState(); }
  }

  async configure() {
    const input = this.q("#token");
    const token = input.value.trim();
    if (!token) { input.focus(); return; }
    const proxy = this.q("#proxy");
    const proxy_url = proxy.value.trim();
    input.value = "";
    proxy.value = "";
    await this.run(() => this._hass.callWS({ type: "habab_telegram/configure", token, proxy_url }));
  }

  async pair() {
    await this.run(async () => {
      const result = await this.request("pair");
      if (!result.pairing_url) throw new Error("pairing_url");
      return { ...this.state, phase: "pairing", pairing_url: result.pairing_url };
    });
  }

  async unlink() {
    await this.run(() => this.request("unlink"), "telegramUnlinked");
  }
}

if (!customElements.get("habab-telegram-panel")) customElements.define("habab-telegram-panel", HababTelegramPanel);
