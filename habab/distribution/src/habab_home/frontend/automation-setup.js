import {strings} from "./strings.js";

// A protected web card owns native configuration writes. The model's local CLI
// can only draft/status; neither a model boolean nor a message confirms a draft.
export class HababAutomationSetup extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this.records = [];
    this.generation = 0;
    this.busy = false;
  }

  get hass() { return this._hass; }
  set hass(value) {
    const owner = value?.user?.is_admin === true ? value.user.id : undefined;
    if (owner !== this.owner) {
      this.owner = owner;
      this._reset();
    }
    this._hass = value;
  }

  get sessionId() { return this._sessionId; }
  set sessionId(value) {
    if (value === this._sessionId) return;
    this._sessionId = value;
    this._reset();
  }

  _reset() {
    this.generation += 1;
    this.records = [];
    this.busy = false;
    this.error = false;
    clearTimeout(this.pollTimer);
    this.render();
  }

  disconnectedCallback() { this._reset(); }
  _t(key) { return (strings[this._hass?.language?.startsWith("ru") ? "ru" : "en"])[key]; }
  _scope() { return {generation: this.generation, hass: this._hass, sessionId: this._sessionId, owner: this.owner}; }
  _valid(scope) {
    return this.isConnected && scope.generation === this.generation &&
      scope.sessionId === this._sessionId && scope.owner === this.owner &&
      (typeof this.isCurrentContext !== "function" || this.isCurrentContext()) &&
      this._hass?.user?.is_admin === true && this._hass.user.id === scope.owner;
  }
  _call(scope, operation, record, extra = {}) {
    return scope.hass.callWS({type: "habab_home/automation/action", operation,
      session_id: scope.sessionId,
      ...(record ? {draft_id: record.draft_id, config_hash: record.config_hash} : {}), ...extra});
  }
  _approved(scope, record, approved) {
    return this._valid(scope) && record.draft_id === approved?.draft_id &&
      record.config_hash === approved.config_hash && record.kind === approved.kind;
  }

  async refresh() {
    const scope = this._scope();
    if (!scope.sessionId || !scope.owner || !this._valid(scope) || this.busy) return;
    this.busy = true;
    try {
      const result = await this._call(scope, "status");
      if (!this._valid(scope)) return;
      if (!Array.isArray(result?.drafts) ||
          (result.recovery_drafts !== undefined && !Array.isArray(result.recovery_drafts))) throw new Error("invalid_receipt");
      this.records = [...result.drafts, ...(result.recovery_drafts || [])];
      this.error = false;
    } catch (_) {
      if (this._valid(scope)) this.error = true;
    } finally {
      if (this._valid(scope)) { this.busy = false; this.render(); }
    }
  }

  async save(record) {
    const scope = this._scope();
    if (!this._valid(scope) || this.busy || record.phase !== "draft" || record.from_previous_chat) return;
    this.busy = true;
    this.error = false;
    this.render();
    try {
      const approved = await this._call(scope, "prepare", record);
      if (!this._valid(scope)) return;
      if (!this._approved(scope, record, approved) || approved.phase !== "saving" ||
          (approved.kind === "automation" ? approved.config?.initial_state !== false ||
            approved.config?.id !== approved.native_id : approved.config?.initial !== false)) throw new Error("invalid_receipt");
      // One attempt only. Even if the native response is lost, the immutable ID
      // and pre-create snapshot let status reconcile without another create.
      this.records = this.records.map((item) => item.draft_id === approved.draft_id ? approved : item);
      if (approved.kind === "automation") {
        await scope.hass.callApi("POST", `config/automation/config/${encodeURIComponent(approved.native_id)}`, approved.config);
      } else {
        const native = await scope.hass.callWS({type: "input_boolean/create", ...approved.config});
        if (!this._valid(scope)) return;
        if (typeof native?.id !== "string" || !native.id) throw new Error("invalid_native_response");
        await this._call(scope, "acknowledge", approved, {native_id: native.id});
      }
    } catch (_) {
      if (this._valid(scope)) this.error = true;
    } finally {
      if (this._valid(scope)) {
        this.busy = false;
        await this.refresh();
        this._reconcile(scope, 8);
      }
    }
  }

  _reconcile(scope, remaining) {
    clearTimeout(this.pollTimer);
    if (!this._valid(scope) || remaining <= 0 ||
        !this.records.some((record) => ["saving", "deleting"].includes(record.phase))) return;
    this.pollTimer = setTimeout(async () => {
      if (!this._valid(scope)) return;
      await this.refresh();
      this._reconcile(scope, remaining - 1);
    }, 2000);
  }

  async cancel(record) {
    const scope = this._scope();
    if (!this._valid(scope) || this.busy) return;
    this.busy = true;
    try { await this._call(scope, "cancel", record); }
    catch (_) { if (this._valid(scope)) this.error = true; }
    finally {
      if (this._valid(scope)) { this.busy = false; await this.refresh(); }
    }
  }

  async dismissAbsent(record) {
    const scope = this._scope();
    if (!this._valid(scope) || this.busy) return;
    this.busy = true;
    try { await this._call(scope, "dismiss_absent", record); }
    catch (_) { if (this._valid(scope)) this.error = true; }
    finally {
      if (this._valid(scope)) { this.busy = false; await this.refresh(); }
    }
  }

  async cleanup(record) {
    const scope = this._scope();
    if (!this._valid(scope) || this.busy || !globalThis.confirm(this._t("automationDeleteConfirm"))) return;
    this.busy = true;
    this.render();
    try {
      const approved = await this._call(scope, "cleanup_prepare", record);
      if (!this._valid(scope)) return;
      if (!this._approved(scope, record, approved) || approved.phase !== "deleting") throw new Error("invalid_receipt");
      this.records = this.records.map((item) => item.draft_id === approved.draft_id ? approved : item);
      if (approved.kind === "automation") {
        await scope.hass.callApi("DELETE", `config/automation/config/${encodeURIComponent(approved.native_id)}`);
      } else {
        await scope.hass.callWS({type: "input_boolean/delete", input_boolean_id: approved.native_id});
      }
    } catch (_) { if (this._valid(scope)) this.error = true; }
    finally {
      if (this._valid(scope)) {
        this.busy = false;
        await this.refresh();
        this._reconcile(scope, 8);
      }
    }
  }

  render() {
    if (!this.shadowRoot) return;
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;color:var(--primary-text-color);font:inherit}
      article{padding-block:var(--ha-space-4,16px);border-block-start:1px solid var(--divider-color)}
      h3,p{margin-block:0 var(--ha-space-2,8px);overflow-wrap:anywhere}
      h3{font-size:1rem}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:.8rem}
      .actions{display:flex;flex-wrap:wrap;gap:var(--ha-space-2,8px);margin-block-start:var(--ha-space-3,12px)}
      button{min-height:44px;padding:var(--ha-space-2,8px);background:var(--card-background-color);color:var(--primary-text-color);border:1px solid var(--divider-color);border-radius:8px;font:inherit;cursor:pointer}
      button:disabled{opacity:.5;cursor:default}button:focus-visible,summary:focus-visible{outline:3px solid var(--primary-color);outline-offset:2px}
      [role=alert]{color:var(--error-color)}
    </style>`;
    for (const record of this.records) {
      const article = document.createElement("article");
      const heading = document.createElement("h3");
      heading.textContent = record.kind === "automation" ? this._t("automationDraft") : record.config.name;
      const plan = document.createElement("p"); plan.textContent = record.summary;
      const message = document.createElement("p");
      const phaseText = {draft: record.kind === "automation" ? "automationReview" : "automationHelperReview",
        saving: "automationPending", saved: "automationSaved", deleting: "automationDeleting",
        deleted: "automationDeleted", cancelled: "automationCancelled", expired: "automationExpired", dismissed: "automationDismissed"};
      message.textContent = this._t(this.error ? "automationFailed" : phaseText[record.phase] || "automationFailed");
      const details = document.createElement("details");
      const summary = document.createElement("summary"); summary.textContent = this._t("automationDetails");
      const config = document.createElement("pre"); config.textContent = JSON.stringify(record.config, null, 2);
      details.append(summary, config);
      const actions = document.createElement("div"); actions.className = "actions";
      const button = (key, action) => {
        const element = document.createElement("button"); element.textContent = this._t(key);
        element.disabled = this.busy; element.addEventListener("click", action); actions.append(element);
      };
      if (record.from_previous_chat) {
        const recovery = document.createElement("p"); recovery.textContent = this._t("automationRecovery"); article.append(recovery);
      }
      if (record.phase === "draft" && !record.from_previous_chat) {
        button(record.kind === "automation" ? "automationSave" : "automationHelperCreate", () => this.save(record));
        button("automationCancel", () => this.cancel(record));
      }
      if (["saving", "deleting", "error"].includes(record.phase)) button("automationCheck", () => this.refresh());
      if (record.phase === "saving" && record.can_dismiss) button("automationDismiss", () => this.dismissAbsent(record));
      if (record.phase === "saved") button("automationDelete", () => this.cleanup(record));
      if (record.entity_id) {
        const entity = document.createElement("p"); entity.textContent = record.entity_id; article.append(entity);
      }
      article.append(heading, plan, message, details, actions);
      this.shadowRoot.append(article);
    }
    if (this.error) {
      const error = document.createElement("p"); error.setAttribute("role", "alert");
      error.textContent = this._t("automationFailed"); this.shadowRoot.append(error);
    }
  }
}

if (!customElements.get("habab-automation-setup")) customElements.define("habab-automation-setup", HababAutomationSetup);
