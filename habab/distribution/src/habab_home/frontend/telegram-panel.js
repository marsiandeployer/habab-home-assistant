import { strings } from "./strings.js";

const OWNER_PHASES = ["not_configured", "pairing", "connected", "error"];
const GROUP_PHASES = [
  "not_configured",
  "pairing",
  "awaiting_admin",
  "connected",
  "error",
];

class HababTelegramPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.state = { phase: "not_configured" };
    this.group = { phase: "unsupported" };
    this.groupPairingUrl = "";
    this.pendingGroupAction = "";
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
    const token = this.q("#token");
    if (token) token.value = "";
    const proxy = this.q("#proxy");
    if (proxy) proxy.value = "";
  }

  t(key, values = {}) {
    let value = strings[this.language || "en"][key] || strings[this.language || "en"].unknown;
    for (const [name, replacement] of Object.entries(values)) {
      value = value.replaceAll(`{${name}}`, String(replacement));
    }
    return value;
  }

  q(selector) {
    return this.shadowRoot.querySelector(selector);
  }

  render() {
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;height:100%;overflow:auto;color:var(--primary-text-color);background:var(--primary-background-color);font:14px/1.5 var(--paper-font-body1_-_font-family,Roboto,system-ui,sans-serif);scrollbar-color:var(--secondary-text-color) transparent;scrollbar-width:thin}
      *{box-sizing:border-box}header{display:flex;align-items:center;gap:16px;min-height:56px;padding:8px 16px;border-block-end:1px solid var(--divider-color);background:var(--card-background-color)}
      h1{font-size:20px;font-weight:500;margin:0}main{max-width:760px;margin:auto;padding:20px 24px 40px}p{max-width:72ch;margin:0 0 12px}.intro{color:var(--secondary-text-color);font-size:14px}
      section{padding-block:16px;border-block-start:1px solid var(--divider-color)}h2{font-size:18px;font-weight:500;margin:0 0 8px}.help{color:var(--secondary-text-color);font-size:13px}.step-label{color:var(--secondary-text-color);font-size:12px;margin-block-end:4px}
      label{display:block;font-weight:500;margin:12px 0 6px}input{width:100%;min-height:48px;padding:10px 12px;border:1px solid var(--secondary-text-color);border-radius:6px;background:var(--card-background-color);color:var(--primary-text-color);font:inherit;caret-color:var(--primary-color)}
      input:focus-visible,button:focus-visible,a:focus-visible,summary:focus-visible{outline:3px solid var(--ha-color-focus,var(--primary-color));outline-offset:3px}
      button{font:inherit;cursor:pointer;min-height:44px;padding:9px 14px;border:1px solid var(--divider-color);border-radius:8px;color:var(--primary-text-color);background:var(--card-background-color)}button:hover{background:var(--secondary-background-color)}button:disabled{cursor:default;opacity:.55}
      .primary{background:var(--ha-color-fill-primary-loud-hover,var(--dark-primary-color));border-color:transparent;color:var(--ha-color-on-primary-loud,var(--text-primary-color))}.danger{color:var(--error-color)}
      .actions{display:flex;flex-wrap:wrap;gap:8px;margin-block:12px}.status{padding:8px 0;overflow-wrap:anywhere}.status[data-error=true]{color:var(--error-color,var(--primary-text-color))}.status[data-success=true]{color:var(--success-color,var(--primary-color))}
      a{color:var(--primary-color);text-underline-offset:3px}.pairing-link{display:inline-block;margin-block:4px 8px}
      details{margin-block:10px}summary{cursor:pointer;min-height:36px;display:flex;align-items:center;color:var(--primary-text-color);text-underline-offset:3px}details p{margin-block:8px}.proxy-field label{margin-block-start:8px}
      .confirmation{padding:12px 0}.confirmation p{font-weight:500}.confirmation .actions{margin-block:8px}[hidden]{display:none!important}
      @media(max-width:600px){header{min-height:52px}main{padding:12px 16px calc(50vh + 24px)}.intro{font-size:13px;margin-block-end:8px}section{padding-block:12px}h2{font-size:17px}label{margin-block-start:8px}.actions{display:grid;grid-template-columns:1fr}.actions button{width:100%}.status{padding-block:6px}.pairing-link{min-height:44px;display:flex;align-items:center}}
    </style>
    <header><ha-menu-button></ha-menu-button><h1>${this.t("telegramTitle")}</h1></header>
    <main>
      <p class="intro">${this.t("telegramIntro")}</p>
      <section aria-labelledby="bot-step-title" id="bot-step">
        <p class="step-label">${this.t("telegramStepOne")}</p>
        <h2 id="bot-step-title">${this.t("telegramBotStepTitle")}</h2>
        <p class="help" id="telegram-security">${this.t("telegramSecurity")}</p>
        <form id="bot-form">
          <label for="token">${this.t("telegramToken")}</label>
          <input id="token" name="token" type="password" required maxlength="512" autocomplete="off" spellcheck="false" autocapitalize="off" aria-describedby="telegram-security">
          <div class="actions"><button class="primary" type="submit">${this.t("telegramConnect")}</button><button type="button" id="cancel-bot-edit" hidden>${this.t("telegramCancel")}</button></div>
          <details id="proxy-options"><summary>${this.t("telegramProxy")}</summary><div class="proxy-field"><label for="proxy">${this.t("telegramProxyAddress")}</label><input id="proxy" name="proxy" type="password" maxlength="2048" autocomplete="off" spellcheck="false" autocapitalize="off" aria-describedby="telegram-proxy-help"><p class="help" id="telegram-proxy-help">${this.t("telegramProxyHelp")}</p></div></details>
        </form>
        <button type="button" id="edit-bot" hidden>${this.t("telegramReplaceBot")}</button>
        <p class="status" id="owner-status" role="status" aria-live="polite"></p>
        <p id="username" hidden></p>
      </section>
      <section aria-labelledby="owner-step-title" id="owner-step" hidden>
        <p class="step-label">${this.t("telegramStepTwo")}</p>
        <h2 id="owner-step-title">${this.t("telegramOwnerStepTitle")}</h2>
        <p class="help">${this.t("telegramOwnerHelp")}</p>
        <p class="status" id="owner-pair-status" role="status" aria-live="polite"></p>
        <a class="pairing-link" id="owner-pairing-action" target="_blank" rel="noopener noreferrer" hidden>${this.t("telegramOpenPairing")}</a>
        <div class="actions"><button type="button" id="owner-pair-action" hidden>${this.t("telegramPair")}</button></div>
      </section>
      <section aria-labelledby="group-step-title" id="group-step">
        <p class="step-label">${this.t("telegramStepThree")}</p>
        <h2 id="group-step-title">${this.t("telegramGroupStepTitle")}</h2>
        <p class="help" id="group-help">${this.t("telegramGroupHelp")}</p>
        <p class="status" id="group-status" role="status" aria-live="polite"></p>
        <a class="pairing-link" id="group-pairing" target="_blank" rel="noopener noreferrer" hidden>${this.t("telegramOpenGroupPairing")}</a>
        <div class="actions"><button class="primary" type="button" id="group-pair" hidden>${this.t("telegramGroupPair")}</button><button class="danger" type="button" id="group-unlink" hidden>${this.t("telegramGroupUnlink")}</button></div>
        <div class="confirmation" id="group-confirmation" role="group" aria-labelledby="group-confirmation-copy" hidden>
          <p id="group-confirmation-copy"></p>
          <div class="actions"><button class="primary" type="button" id="group-confirm-action">${this.t("telegramConfirm")}</button><button type="button" id="group-cancel-action">${this.t("telegramCancel")}</button></div>
        </div>
        <p class="status" id="group-action-status" role="status" aria-live="polite"></p>
        <details id="group-guidance"><summary>${this.t("telegramGroupDetailsSummary")}</summary><p class="help">${this.t("telegramGroupSteps")}</p></details>
      </section>
      <details id="owner-management"><summary>${this.t("telegramMoreOptions")}</summary>
        <p class="help">${this.t("telegramOwnerAccessHelp")}</p>
        <div class="actions"><button type="button" id="refresh">${this.t("telegramRefresh")}</button><button class="danger" type="button" id="owner-unlink">${this.t("telegramUnlink")}</button></div>
        <div class="confirmation" id="owner-confirmation" role="group" aria-labelledby="owner-confirmation-copy" hidden>
          <p id="owner-confirmation-copy">${this.t("telegramOwnerUnlinkConfirm")}</p>
          <div class="actions"><button class="danger" type="button" id="owner-confirm-action">${this.t("telegramConfirm")}</button><button type="button" id="owner-cancel-action">${this.t("telegramCancel")}</button></div>
        </div>
      </details>
    </main>`;
    this.q("ha-menu-button").hass = this._hass;
    this.q("#bot-form").addEventListener("submit", (event) => {
      event.preventDefault();
      this.configure();
    });
    this.q("#owner-pair-action").addEventListener("click", () => this.pair());
    this.q("#refresh").addEventListener("click", () => this.refresh());
    this.q("#edit-bot").addEventListener("click", () => {
      this.editingBot = true;
      this.applyState();
      this.q("#token").focus();
    });
    this.q("#cancel-bot-edit").addEventListener("click", () => {
      this.editingBot = false;
      this.q("#token").value = "";
      this.q("#proxy").value = "";
      this.applyState();
      this.q("#edit-bot").focus();
    });
    this.q("#group-pair").addEventListener("click", () => this.requestGroupPair());
    this.q("#group-unlink").addEventListener("click", () => this.askGroupConfirmation("unlink"));
    this.q("#group-confirm-action").addEventListener("click", () => this.confirmGroupAction());
    this.q("#group-cancel-action").addEventListener("click", () => this.cancelGroupConfirmation());
    this.q("#owner-unlink").addEventListener("click", () => this.askOwnerConfirmation());
    this.q("#owner-confirm-action").addEventListener("click", () => this.unlinkOwner());
    this.q("#owner-cancel-action").addEventListener("click", () => this.cancelOwnerConfirmation());
    this.applyState();
  }

  applyState() {
    if (!this.shadowRoot.firstChild) return;
    const ownerStatus = this.q("#owner-status");
    const ownerPhaseKey = {
      not_configured: "telegramNotConfigured",
      pairing: "telegramOwnerPairing",
      connected: "telegramOwnerConnected",
      error: "telegramError",
    }[this.state.phase] || "telegramError";
    ownerStatus.textContent = this.ownerFeedback || (this.state.phase === "error"
      ? this.ownerErrorText(this.state.error)
      : this.t(ownerPhaseKey));
    ownerStatus.dataset.error = String(this.state.phase === "error");

    const username = this.q("#username");
    username.hidden = !this.state.bot_username;
    username.textContent = this.state.bot_username
      ? `${this.t("telegramUsername")}: @${this.state.bot_username.replace(/^@/, "")}`
      : "";
    const ownerPairingAction = this.q("#owner-pairing-action");
    ownerPairingAction.hidden = !this.state.pairing_url;
    ownerPairingAction.href = this.state.pairing_url || "#";
    const ownerCanPair = this.state.phase === "pairing";
    this.q("#owner-step").hidden = this.state.phase === "not_configured" || this.state.phase === "error";
    this.q("#owner-pair-action").hidden = !ownerCanPair;
    this.q("#owner-pair-status").textContent = this.t(
      this.state.phase === "connected" ? "telegramOwnerConnected"
        : this.state.phase === "pairing" ? "telegramOwnerPairing"
          : "telegramOwnerNotReady",
    );
    const canEditBot = ["pairing", "connected"].includes(this.state.phase);
    this.q("#bot-form").hidden = canEditBot && !this.editingBot;
    this.q("#edit-bot").hidden = !canEditBot || Boolean(this.editingBot);
    this.q("#cancel-bot-edit").hidden = !this.editingBot;
    if (!canEditBot) this.editingBot = false;

    this.applyGroupState();
    this.updateControls();
    this.scheduleStatusPoll();
  }

  applyGroupState() {
    const status = this.q("#group-status");
    const title = this.group.title ? ` «${this.group.title}»` : "";
    const errorKey = this.groupErrorKey(this.group.error);
    const groupStatusKey = {
      unsupported: "telegramGroupUnsupported",
      not_configured: "telegramGroupNotConfigured",
      pairing: "telegramGroupPairing",
      awaiting_admin: this.group.error ? errorKey : "telegramGroupAwaitingAdmin",
      connected: "telegramGroupConnected",
      error: errorKey,
    }[this.group.phase] || "telegramGroupUnsupported";
    const replacementPending = this.group.phase === "connected" && Boolean(this.group.pairing_pending);
    const pendingStatus = replacementPending
      ? this.group.pending_phase === "awaiting_admin"
        ? this.t(this.groupErrorKey(this.group.pending_error))
        : this.t("telegramGroupReplacementPairing")
      : this.t(groupStatusKey);
    status.textContent = `${pendingStatus}${this.group.phase === "connected" ? title : ""}${replacementPending ? ` ${this.t("telegramGroupPreviousStillActive")}` : ""}`;
    status.dataset.error = String(this.group.phase === "error" || Boolean(this.group.error) || Boolean(replacementPending && this.group.pending_error));
    status.dataset.success = String(this.group.phase === "connected" && !replacementPending);
    const ownerReady = this.state.phase === "connected";
    this.q("#group-help").textContent = this.t(
      ownerReady ? "telegramGroupHelp" : "telegramGroupLockedHelp",
    );
    const groupPair = this.q("#group-pair");
    groupPair.hidden = !ownerReady || this.group.phase === "unsupported";
    groupPair.textContent = this.t(
      replacementPending ? "telegramGroupRefreshAction"
        : this.group.phase === "connected" ? "telegramGroupReplaceAction"
        : ["pairing", "awaiting_admin", "error"].includes(this.group.phase) ? "telegramGroupRefreshAction"
          : "telegramGroupPair",
    );
    this.q("#group-unlink").hidden = !ownerReady || this.group.phase !== "connected";
    const groupPairing = this.q("#group-pairing");
    const linkWasUsed = this.group.phase === "awaiting_admin"
      || Boolean(replacementPending && this.group.pending_phase === "awaiting_admin");
    groupPairing.hidden = !this.groupPairingUrl || this.busy || linkWasUsed;
    groupPairing.href = this.groupPairingUrl || "#";
    this.q("#group-status").setAttribute("aria-busy", String(this.busy));
    this.q("#owner-unlink").hidden = this.state.phase === "not_configured";

    const confirmation = this.q("#group-confirmation");
    confirmation.hidden = !this.pendingGroupAction;
    if (this.pendingGroupAction === "pair") {
      const key = this.group.phase === "connected" && !this.group.pairing_pending
        ? "telegramGroupReplaceConfirm"
        : "telegramGroupPairRefreshConfirm";
      this.q("#group-confirmation-copy").textContent = this.t(key);
    } else if (this.pendingGroupAction === "unlink") {
      this.q("#group-confirmation-copy").textContent = this.t("telegramGroupUnlinkConfirm");
    }
  }

  ownerErrorText(error) {
    const key = {
      invalid_token: "telegramInvalidToken",
      invalid_proxy: "telegramInvalidProxy",
      cannot_validate: "telegramCannotValidate",
      setup_failed: "telegramWorkerFailed",
      worker_error: "telegramWorkerFailed",
      worker_stopped: "telegramWorkerFailed",
      worker_protocol_error: "telegramWorkerFailed",
      not_configured: "telegramNotConfiguredError",
      pairing_url: "telegramPairUnavailable",
    }[error] || "telegramUnknownError";
    return this.t(key);
  }

  groupErrorKey(error) {
    return {
      not_configured: "telegramGroupNotConfigured",
      not_allowed: "telegramGroupNotAllowed",
      worker_not_ready: "telegramGroupWorkerNotReady",
      telegram_group_not_found: "telegramGroupNotFound",
      telegram_group_invalid: "telegramGroupInvalid",
      telegram_group_pair_expired: "telegramGroupPairExpired",
      telegram_group_busy: "telegramGroupBusy",
      bot_not_admin: "telegramGroupBotNotAdmin",
      verification_unavailable: "telegramGroupVerificationUnavailable",
    }[error] || "telegramGroupError";
  }

  updateControls() {
    if (!this.shadowRoot.firstChild) return;
    this.shadowRoot.querySelectorAll("button,input").forEach((element) => {
      element.disabled = this.busy;
    });
    const confirmButton = this.q("#group-confirm-action");
    if (confirmButton) confirmButton.disabled = this.busy;
    const ownerConfirmButton = this.q("#owner-confirm-action");
    if (ownerConfirmButton) ownerConfirmButton.disabled = this.busy;
  }

  async request(message, payload = {}) {
    return this._hass.callWS({ type: `habab_telegram/${message}`, ...payload });
  }

  async refresh() {
    if (!this._hass || this.busy || this.refreshing) return;
    this.refreshing = true;
    this.ownerFeedback = "";
    try {
      const result = await this.request("status");
      if (!OWNER_PHASES.includes(result.phase)) throw new Error("status");
      this.state = result;
      this.group = result.group && GROUP_PHASES.includes(result.group.phase)
        ? result.group
        : { phase: "unsupported" };
      if (this.group.phase === "connected") this.groupPairingUrl = "";
      this.applyState();
    } catch (_) {
      this.state = { phase: "error", error: "unknown" };
      this.group = { phase: "unsupported" };
      this.applyState();
    } finally {
      this.refreshing = false;
    }
  }

  scheduleStatusPoll() {
    clearTimeout(this.statusTimer);
    const ownerPending = this.state.phase === "pairing";
    const groupPending = ["pairing", "awaiting_admin"].includes(this.group.phase)
      || Boolean(this.group.pairing_pending);
    if (this.isConnected && (ownerPending || groupPending)) {
      this.statusTimer = setTimeout(() => this.refresh(), 2500);
    }
  }

  async run(action, successText) {
    if (!this._hass || this.busy) return;
    this.busy = true;
    this.ownerFeedback = "";
    this.updateControls();
    try {
      const result = await action();
      if (result?.phase) this.state = result;
      else await this.loadStatus();
      if (successText) this.ownerFeedback = this.t(successText);
    } catch (error) {
      this.state = { phase: "error", error: error?.code || error?.message || "unknown" };
    } finally {
      this.busy = false;
      this.applyState();
    }
  }

  async loadStatus() {
    const result = await this.request("status");
    if (!OWNER_PHASES.includes(result.phase)) throw new Error("status");
    this.state = result;
    this.group = result.group && GROUP_PHASES.includes(result.group.phase)
      ? result.group
      : { phase: "unsupported" };
    if (this.group.phase === "connected") this.groupPairingUrl = "";
  }

  async configure() {
    const input = this.q("#token");
    const token = input.value.trim();
    if (!token) {
      input.focus();
      return;
    }
    const proxy = this.q("#proxy");
    const proxy_url = proxy.value.trim();
    input.value = "";
    proxy.value = "";
    this.groupPairingUrl = "";
    this.ownerFeedback = "";
    await this.run(() => this.request("configure", { token, proxy_url }));
  }

  async pair() {
    await this.run(async () => {
      const result = await this.request("pair");
      if (!result.pairing_url) throw new Error("pairing_url");
      return { ...this.state, phase: "pairing", pairing_url: result.pairing_url };
    });
  }

  requestGroupPair() {
    if (this.state.phase !== "connected" || this.busy) return;
    if (["connected", "pairing", "awaiting_admin", "error"].includes(this.group.phase)) {
      this.askGroupConfirmation("pair");
      return;
    }
    this.generateGroupPairingLink();
  }

  askGroupConfirmation(action) {
    this.pendingGroupAction = action;
    this.q("#group-action-status").textContent = "";
    this.applyGroupState();
    this.q("#group-confirm-action").focus();
  }

  cancelGroupConfirmation() {
    this.pendingGroupAction = "";
    this.applyGroupState();
    this.q("#group-pair").focus();
  }

  async confirmGroupAction() {
    const action = this.pendingGroupAction;
    if (!action || this.busy) return;
    this.pendingGroupAction = "";
    if (action === "pair") await this.generateGroupPairingLink();
    else if (action === "unlink") await this.unlinkGroup();
  }

  async generateGroupPairingLink() {
    if (!this._hass || this.state.phase !== "connected" || this.busy) return;
    this.busy = true;
    this.q("#group-action-status").dataset.error = "false";
    this.q("#group-action-status").dataset.success = "false";
    this.q("#group-action-status").textContent = this.t("telegramGroupLinkCreating");
    this.updateControls();
    try {
      const result = await this.request("group_pair");
      const url = new URL(result.group_pairing_url);
      if (url.protocol !== "https:" || url.hostname !== "t.me" || !url.searchParams.has("startgroup")) {
        throw new Error("group_pairing_url");
      }
      this.groupPairingUrl = url.toString();
      this.q("#group-action-status").textContent = this.t("telegramGroupLinkReady");
      await this.loadStatus();
    } catch (error) {
      this.q("#group-action-status").textContent = this.t(this.groupErrorKey(error?.code || error?.message));
      this.q("#group-action-status").dataset.error = "true";
    } finally {
      this.busy = false;
      this.applyState();
    }
  }

  async unlinkGroup() {
    if (!this._hass || this.busy) return;
    this.busy = true;
    this.q("#group-action-status").dataset.error = "false";
    this.q("#group-action-status").dataset.success = "false";
    this.updateControls();
    try {
      await this.request("unlink_group");
      this.groupPairingUrl = "";
      this.pendingGroupAction = "";
      await this.loadStatus();
      this.q("#group-action-status").textContent = this.t("telegramGroupUnlinked");
      this.q("#group-action-status").dataset.success = "true";
    } catch (error) {
      this.q("#group-action-status").textContent = this.t(this.groupErrorKey(error?.code || error?.message));
      this.q("#group-action-status").dataset.error = "true";
    } finally {
      this.busy = false;
      this.applyState();
    }
  }

  askOwnerConfirmation() {
    this.q("#owner-confirmation").hidden = false;
    this.q("#owner-confirm-action").focus();
  }

  cancelOwnerConfirmation() {
    this.q("#owner-confirmation").hidden = true;
    this.q("#owner-unlink").focus();
  }

  async unlinkOwner() {
    this.q("#owner-confirmation").hidden = true;
    await this.run(() => this.request("unlink"), "telegramUnlinked");
  }

}

if (!customElements.get("habab-telegram-panel")) {
  customElements.define("habab-telegram-panel", HababTelegramPanel);
}
