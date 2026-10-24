const DOMAIN_PATTERN = /^[a-z0-9_]{1,80}$/;
const ERROR_PATTERN = /^[a-z0-9_]{1,80}$/;
const PHASES = new Set([
  "form", "menu", "external", "progress", "done", "abort", "error", "expired",
]);
const TERMINAL_PHASES = new Set(["done", "abort", "error", "expired"]);
const POLL_INTERVAL = 2500;

export function safeExternalUrl(value, base = globalThis.location?.href) {
  if (typeof value !== "string" || value.length > 4096) return undefined;
  try {
    const parsed = new URL(value, base);
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) {
      return undefined;
    }
    return parsed.href;
  } catch {
    return undefined;
  }
}

export function stripFormDefaults(value) {
  if (Array.isArray(value)) return value.map(stripFormDefaults);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value)
    .filter(([key]) => key !== "default" && key !== "suggested_value")
    .map(([key, item]) => [key, stripFormDefaults(item)]));
}

function safeErrorMap(errors, fields = new Set()) {
  if (!errors || typeof errors !== "object" || Array.isArray(errors)) return {};
  return Object.fromEntries(Object.entries(errors).filter(([field, value]) =>
    (field === "base" || fields.has(field)) && typeof value === "string" && ERROR_PATTERN.test(value)
  ));
}

function safePlaceholders(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return Object.fromEntries(Object.entries(value).slice(0, 50).filter(([key, item]) =>
    /^[a-zA-Z0-9_.-]{1,100}$/.test(key) &&
    ((typeof item === "string" && item.length <= 1000) ||
      (typeof item === "number" && Number.isFinite(item)) || typeof item === "boolean")
  ));
}

function normalizeInteraction(value) {
  if (!value || typeof value.id !== "string" || !value.id ||
      typeof value.domain !== "string" || !DOMAIN_PATTERN.test(value.domain) ||
      !PHASES.has(value.phase) || !["config", "options"].includes(value.kind)) return undefined;
  const fields = Array.isArray(value.data_schema)
    ? value.data_schema.filter((field) => field && typeof field.name === "string")
    : [];
  const result = {
    id: value.id,
    domain: value.domain,
    kind: value.kind,
    phase: value.phase,
    data_schema: stripFormDefaults(fields),
    errors: safeErrorMap(value.errors, new Set(fields.map((field) => field.name))),
  };
  if (typeof value.handler === "string" && DOMAIN_PATTERN.test(value.handler)) result.handler = value.handler;
  if (typeof value.translation_domain === "string" && DOMAIN_PATTERN.test(value.translation_domain)) {
    result.translation_domain = value.translation_domain;
  }
  if (typeof value.last_step === "boolean") result.last_step = value.last_step;
  if (value.description_placeholders) result.description_placeholders = safePlaceholders(value.description_placeholders);
  if (typeof value.entry_id === "string" && value.entry_id.length <= 256) result.entry_id = value.entry_id;
  if (typeof value.step_id === "string" && DOMAIN_PATTERN.test(value.step_id)) result.step_id = value.step_id;
  if (typeof value.title === "string") result.title = value.title.slice(0, 300);
  if (Array.isArray(value.menu_options)) {
    result.menu_options = value.menu_options.filter((option) =>
      option && typeof option.value === "string" && option.value.length > 0 && option.value.length <= 256 &&
      typeof option.label === "string"
    ).map(({value: optionValue, label}) => ({value: optionValue, label: label.slice(0, 300)}));
  }
  const externalUrl = safeExternalUrl(value.url);
  if (externalUrl) result.url = externalUrl;
  if (typeof value.entry_state === "string") result.entry_state = value.entry_state;
  if (Number.isInteger(value.entity_count) && value.entity_count >= 0) result.entity_count = value.entity_count;
  if (typeof value.progress === "number" && Number.isFinite(value.progress) && value.progress >= 0 && value.progress <= 1) {
    result.progress = value.progress;
  }
  return result;
}

function schemaKey(schema) {
  try { return JSON.stringify(schema); } catch { return "invalid-schema"; }
}

export class HababIntegrationSetup extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this._hass = undefined;
    this._active = false;
    this.interactions = [];
    this.cards = new Map();
    this.busy = new Set();
    this.loading = false;
    this.pollTimer = undefined;
    this.statusError = undefined;
    this.lifecycle = 0;
    this.refreshPending = false;
  }

  get hass() { return this._hass; }
  set hass(value) {
    const previous = this._hass;
    this._hass = value;
    for (const card of this.cards.values()) {
      if (card.form) card.form.hass = value;
      if (card.context) card.context.hass = value;
    }
    const connected = value?.connected !== false;
    const initialConnection = !previous && this.isConnected;
    const reconnected = previous?.connected === false && connected;
    const languageChanged = previous && previous.language !== value?.language;
    if (connected && this.isConnected && (initialConnection || reconnected || languageChanged)) this.refresh();
  }

  get active() { return this._active; }
  set active(value) {
    this._active = Boolean(value);
    if (this._active) this.refresh();
    this.schedulePoll();
  }

  connectedCallback() {
    this._ensureShell();
    this.refresh();
  }

  disconnectedCallback() {
    this.lifecycle += 1;
    this.refreshPending = false;
    clearTimeout(this.pollTimer);
    this.pollTimer = undefined;
    for (const card of this.cards.values()) {
      this._disposeForm(card);
    }
    this._cardsRoot?.replaceChildren();
    if (this._statusError) {
      this._statusError.hidden = true;
      this._statusError.textContent = "";
    }
    this.cards.clear();
    this.interactions = [];
    this.busy.clear();
    this.statusError = undefined;
  }

  _language() {
    const language = this._hass?.language;
    return typeof language === "string" && /^[a-zA-Z0-9_-]{2,20}$/.test(language) ? language : "en";
  }

  _copy(ru, en) {
    return this._language().toLowerCase().startsWith("ru") ? ru : en;
  }

  _localize(key, values, fallback) {
    const translated = this._hass?.localize?.(key, values);
    return typeof translated === "string" && translated !== key && translated
      ? translated
      : fallback;
  }

  _ensureShell() {
    if (this.shadowRoot.firstChild) return;
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;margin-block:16px;color:var(--primary-text-color);font:inherit}
      *{box-sizing:border-box}.cards{display:grid;gap:16px}
      article{min-width:0;padding-block:14px;border-block-start:1px solid var(--divider-color,#d8d8d8)}
      article:first-child{border-block-start:0;padding-block-start:0}
      h3{margin:0 0 10px;font-size:16px;font-weight:500;line-height:1.35;overflow-wrap:anywhere}
      .message{margin:8px 0;color:var(--secondary-text-color);overflow-wrap:anywhere}
      .message[role=alert]{color:var(--error-color,#b3261e)}
      .body{min-width:0}step-flow-form{display:block}
      .options{display:grid;gap:8px;margin-block:10px}
      button{min-height:44px;padding:8px 14px;border:1px solid var(--divider-color,#d8d8d8);border-radius:8px;background:var(--card-background-color,#fff);color:var(--primary-text-color);font:inherit;cursor:pointer;text-align:start}
      button:hover{background:var(--secondary-background-color,#f1f3f4)}
      button.primary{background:var(--primary-color,#007a9e);border-color:transparent;color:var(--text-primary-color,#fff)}
      button:disabled{opacity:.55;cursor:default}
      :is(button,a,step-flow-form):focus-visible{outline:3px solid var(--ha-color-focus,var(--primary-color,#007a9e));outline-offset:3px}
      .actions{display:flex;flex-wrap:wrap;gap:8px;margin-block-start:12px}
      a{color:var(--primary-color,#007a9e);text-underline-offset:3px}
      progress{display:block;width:100%;height:8px;margin-block:10px;accent-color:var(--primary-color,#007a9e)}
      .status{font-size:14px;line-height:1.5}
      ::selection{background:var(--primary-color,#007a9e);color:var(--text-primary-color,#fff)}
    </style><div class="cards" role="region" aria-live="polite"></div><div class="status-error" role="alert" hidden></div>`;
    this._cardsRoot = this.shadowRoot.querySelector(".cards");
    this._statusError = this.shadowRoot.querySelector(".status-error");
  }

  _nativeFormBridge() {
    const bridge = globalThis.window?.hababNativeUi || globalThis.hababNativeUi;
    if (!bridge || bridge.version !== 1 || typeof bridge.loadFlowForm !== "function") {
      throw new Error("native_form_unavailable");
    }
    return bridge;
  }

  async refresh() {
    if (!this._hass || this._hass.connected === false) return;
    if (this.loading) {
      this.refreshPending = true;
      return;
    }
    this._ensureShell();
    this.loading = true;
    const lifecycle = this.lifecycle;
    this.statusError = undefined;
    try {
      const result = await this._hass.callWS({type: "habab_home/setup/status", language: this._language()});
      if (lifecycle !== this.lifecycle || !this.isConnected) return;
      const interactions = Array.isArray(result?.interactions) ? result.interactions : [];
      this._applyInteractions(interactions);
      if (lifecycle !== this.lifecycle || !this.isConnected) return;
      this._render();
    } catch (error) {
      if (lifecycle === this.lifecycle && this.isConnected) {
        this.statusError = this._errorText("", this._safeCode(error));
        this._render();
      }
    } finally {
      this.loading = false;
      if (this.refreshPending && this.isConnected && this._hass?.connected !== false) {
        this.refreshPending = false;
        queueMicrotask(() => this.refresh());
      } else {
        this.refreshPending = false;
        this.schedulePoll();
      }
    }
  }

  load() { return this.refresh(); }

  revealInteraction(id) {
    const card = this.cards.get(id);
    if (!this.isConnected || !card || card.record?.phase !== "form" ||
        !card.form || card.form.isConnected === false || card.element.isConnected === false ||
        typeof card.element.scrollIntoView !== "function") return false;
    card.element.scrollIntoView({block: "nearest"});
    return true;
  }

  _safeCode(error) {
    const code = error?.code;
    return typeof code === "string" && ERROR_PATTERN.test(code) ? code : "setup_failed";
  }

  _applyInteractions(values) {
    const next = values.map(normalizeInteraction).filter(Boolean);
    const nextIds = new Set(next.map((item) => item.id));
    for (const [id, card] of this.cards) {
      if (!nextIds.has(id)) {
        card.element.remove();
        this.cards.delete(id);
        this._disposeCard(card);
      }
    }
    for (const interaction of next) this._applyInteraction(interaction);
    this.interactions = next;
  }

  _applyInteraction(interaction) {
    const id = interaction.id;
    let card = this.cards.get(id);
    if (!card) {
      card = this._createCard(id);
      this.cards.set(id, card);
    }
    const nextSchemaKey = schemaKey(interaction.data_schema);
    const phaseChanged = card.phase !== interaction.phase;
    const domainChanged = card.record?.domain !== interaction.domain;
    const schemaChanged = card.schemaKey !== nextSchemaKey;
    const stepChanged = card.record?.step_id !== interaction.step_id;
    const renderChanged = phaseChanged || domainChanged || schemaChanged || stepChanged || card.kind !== interaction.kind;
    if (renderChanged && card.form) this._disposeForm(card);
    card.record = interaction;
    card.phase = interaction.phase;
    card.kind = interaction.kind;
    card.schemaKey = nextSchemaKey;
    card.title.textContent = interaction.title || this._integrationTitle(interaction.domain);
    if (renderChanged) this._renderPhase(card);
    else this._updateForm(card);
    this._updateBusy(card);
    if (phaseChanged) this._emitState(interaction.phase, interaction.domain);
  }

  _createCard(id) {
    const element = document.createElement("article");
    const title = document.createElement("h3");
    const body = document.createElement("div"); body.className = "body";
    const actions = document.createElement("div"); actions.className = "actions";
    element.append(title, body, actions);
    return {id, element, title, body, actions, phase: undefined, kind: undefined, schemaKey: undefined, form: undefined};
  }

  _render() {
    this._ensureShell();
    this._statusError.hidden = !this.statusError;
    this._statusError.textContent = this.statusError || "";
    const ordered = this.interactions.map(({id}) => this.cards.get(id)?.element).filter(Boolean);
    for (const element of ordered) {
      if (element.parentNode !== this._cardsRoot) this._cardsRoot.append(element);
    }
  }

  _renderPhase(card) {
    card.body.replaceChildren();
    card.actions.replaceChildren();
    card.form = undefined;
    card.submitButton = undefined;
    const record = card.record;
    if (record.phase === "form") {
      if (card.flowConfig) {
        this._renderForm(card);
        card.submitButton = this._button(card, card.actions, this._nativeSubmitLabel(card), () => this._submitNativeForm(card), true);
        this._button(card, card.actions, this._localize("ui.common.cancel", undefined, this._copy("Отмена", "Cancel")), () => this.cancel(record.id));
        return;
      }
      if (card.formLoadError) {
        this._text(card.body, this._copy("Не удалось загрузить форму настройки.", "The setup form could not be loaded."), "error");
        this._button(card, card.actions, this._localize("ui.common.retry", undefined, this._copy("Повторить", "Retry")), () => this._loadNativeForm(card), true);
        this._button(card, card.actions, this._localize("ui.common.cancel", undefined, this._copy("Отмена", "Cancel")), () => this.cancel(record.id));
        return;
      }
      this._text(card.body, this._copy("Загружаем форму настройки…", "Loading setup form…"), "status");
      this._button(card, card.actions, this._localize("ui.common.cancel", undefined, this._copy("Отмена", "Cancel")), () => this.cancel(record.id));
      this._loadNativeForm(card);
      return;
    }
    if (record.phase === "menu") {
      const options = document.createElement("div"); options.className = "options";
      for (const option of record.menu_options || []) {
        this._button(card, options, option.label, () => this.submit(record.id, {next_step_id: option.value}));
      }
      card.body.append(options);
      this._button(card, card.actions, this._localize("ui.common.cancel", undefined, this._copy("Отмена", "Cancel")), () => this.cancel(record.id));
      return;
    }
    if (record.phase === "external") {
      this._text(card.body, this._localize("ui.panel.config.integrations.config_flow.external_step.description", undefined, this._copy("Продолжите этот шаг на сайте интеграции.", "Continue this step on the integration website.")));
      const url = safeExternalUrl(record.url);
      if (url) {
        const link = document.createElement("a");
        link.textContent = this._localize("ui.panel.config.integrations.config_flow.external_step.open_site", undefined, this._copy("Открыть сайт", "Open website"));
        link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
        card.body.append(link);
      }
      this._button(card, card.actions, this._localize("ui.common.cancel", undefined, this._copy("Отмена", "Cancel")), () => this.cancel(record.id));
      return;
    }
    if (record.phase === "progress") {
      const message = this._entryStateText(record.entry_state) || this._localize(
        "ui.panel.config.integrations.config_entry.state.setup_in_progress", undefined, this._copy("Настраиваем интеграцию…", "Setting up integration…")
      );
      this._text(card.body, message, "status");
      if (record.progress !== undefined) {
        const progress = document.createElement("progress"); progress.max = 1; progress.value = record.progress;
        progress.setAttribute("aria-label", message); card.body.append(progress);
      }
      if (!record.entry_state) this._button(card, card.actions, this._localize("ui.common.cancel", undefined, this._copy("Отмена", "Cancel")), () => this.cancel(record.id));
      return;
    }
    if (record.phase === "done") {
      this._text(card.body, this._entryStateText(record.entry_state) || this._localize(
        "ui.panel.config.integrations.config_entry.state.loaded", undefined, this._copy("Интеграция подключена", "Integration loaded")
      ), "status");
      if (record.entity_count !== undefined) {
        this._text(card.body, this._localize("ui.panel.config.integrations.config_entry.entities", {count: record.entity_count}, this._copy(`Сущностей: ${record.entity_count}`, `${record.entity_count} entities`)));
      }
      this._button(card, card.actions, this._copy("Проверить подключение", "Verify connection"), () => this._verify(record.domain), true);
      this._button(card, card.actions, this._localize("ui.common.close", undefined, this._copy("Закрыть", "Close")), () => this.dismiss(record.id));
      return;
    }
    const errorCode = record.errors?.base;
    const message = record.phase === "abort"
      ? this._localize("ui.panel.config.integrations.config_flow.aborted", undefined, this._copy("Настройка отменена.", "Setup canceled."))
      : record.phase === "expired"
        ? this._copy("Время ожидания этого шага истекло. Начните подключение снова из чата.", "This setup step expired. Start setup again from the chat.")
        : this._errorText(record.domain, errorCode || "setup_failed");
    this._text(card.body, message, "error");
    this._button(card, card.actions, this._localize("ui.common.close", undefined, this._copy("Закрыть", "Close")), () => this.dismiss(record.id));
  }

  _nativeSubmitLabel(card) {
    const label = card.flowConfig?.renderShowFormStepSubmitButton?.(this._hass, this._nativeStep(card.record));
    return typeof label === "string" && label.trim() ? label : this._copy("Продолжить", "Continue");
  }

  async _submitNativeForm(card) {
    if (!card.form || card.nativeSubmitPending || card.formLoading || this.busy.has(card.id)) return;
    card.nativeSubmitPending = true;
    this._updateNativeSubmitButton(card);
    try {
      await card.form.submit();
    } catch {
      // StepFlowForm renders controller and validation errors itself.
    } finally {
      card.nativeSubmitPending = false;
      this._updateNativeSubmitButton(card);
    }
  }

  _updateNativeSubmitButton(card) {
    if (card.submitButton) {
      card.submitButton.disabled = this.busy.has(card.id) || Boolean(card.formLoading || card.nativeSubmitPending);
    }
  }

  _nativeStep(record) {
    const common = {
      flow_id: record.id,
      handler: record.kind === "options" ? record.entry_id : record.handler || record.domain,
      ...(record.translation_domain ? {translation_domain: record.translation_domain} : {}),
      ...(record.description_placeholders ? {
        description_placeholders: Object.fromEntries(Object.entries(record.description_placeholders)
          .map(([key, value]) => [key, String(value)])),
      } : {}),
    };
    if (record.phase === "form") return {
      ...common,
      type: "form",
      step_id: record.step_id || "user",
      data_schema: record.data_schema,
      errors: record.errors,
      last_step: record.last_step ?? null,
    };
    if (record.phase === "menu") return {
      ...common,
      type: "menu",
      step_id: record.step_id || "user",
      menu_options: (record.menu_options || []).map((option) => option.value),
    };
    if (record.phase === "external" && record.url) return {
      ...common,
      type: "external",
      step_id: record.step_id || "external",
      url: record.url,
    };
    if (record.phase === "progress") return {
      ...common,
      type: "progress",
      step_id: record.step_id || "setup",
      progress_action: "setup",
    };
    if (record.phase === "done") return {
      ...common,
      type: "create_entry",
      title: record.title || this._integrationTitle(record.domain),
      description: null,
    };
    return {
      ...common,
      type: "abort",
      reason: record.errors?.base || (record.phase === "expired" ? "flow_expired" : "setup_failed"),
    };
  }

  async _loadNativeForm(card) {
    if (card.flowLoading || card.flowConfig || card.record.phase !== "form") return;
    const record = card.record;
    const lifecycle = this.lifecycle;
    const schemaKeyAtLoad = card.schemaKey;
    const loadToken = {};
    card.formLoadError = undefined;
    card.flowLoading = loadToken;
    try {
      const bridge = this._nativeFormBridge();
      const nativeForm = await bridge.loadFlowForm({
        hass: this._hass,
        flowType: record.kind === "options" ? "options_flow" : "config_flow",
        handler: record.handler || record.domain,
        domain: record.domain,
        step: this._nativeStep(record),
        ...(record.entry_id ? {entryId: record.entry_id} : {}),
        submit: (hass, interactionId, input) => this._submitNativeStep(card, lifecycle, hass, interactionId, input),
      });
      if (lifecycle !== this.lifecycle || !this.isConnected || this.cards.get(card.id) !== card ||
          card.flowLoading !== loadToken ||
          card.schemaKey !== schemaKeyAtLoad || card.record.phase !== "form") return;
      if (!nativeForm || typeof nativeForm.tag !== "string" || typeof nativeForm.contextTag !== "string" ||
          !nativeForm.flowConfig || !customElements.get(nativeForm.tag) || !customElements.get(nativeForm.contextTag)) {
        throw new Error("native_form_unavailable");
      }
      card.nativeForm = nativeForm;
      card.flowConfig = nativeForm.flowConfig;
      card.formLoadError = undefined;
      this._renderPhase(card);
      this._render();
      const form = card.form;
      if (!form) return;
      try {
        await form.updateComplete;
      } catch {
        return;
      }
      if (lifecycle !== this.lifecycle || !this.isConnected ||
          this.cards.get(card.id) !== card || card.form !== form ||
          card.record.phase !== "form" || form.isConnected === false ||
          card.readyForm === form) return;
      card.readyForm = form;
      this.dispatchEvent(new CustomEvent("habab-setup-form-ready", {
        bubbles: true,
        composed: true,
        detail: {interaction_id: card.id, domain: card.record.domain},
      }));
    } catch (error) {
      if (lifecycle === this.lifecycle && card.flowLoading === loadToken && this.isConnected && this.cards.get(card.id) === card) {
        card.formLoadError = this._safeCode(error);
        this._renderPhase(card);
        this._render();
      }
    } finally {
      if (card.flowLoading === loadToken) card.flowLoading = undefined;
    }
  }

  _renderForm(card) {
    const record = card.record;
    const context = document.createElement(card.nativeForm.contextTag);
    const form = document.createElement(card.nativeForm.tag);
    context.hass = this._hass;
    context.narrow = true;
    form.hass = this._hass;
    form.flowConfig = card.flowConfig;
    form.step = this._nativeStep(record);
    form.domain = record.domain;
    form.narrow = true;
    context.addEventListener("value-changed", (event) => event.stopPropagation());
    form.addEventListener("flow-update", (event) => {
      event.stopPropagation();
      const next = card.pendingInteraction;
      card.pendingInteraction = undefined;
      if (next) this._applyOne(next);
      else this.refresh();
    });
    form.addEventListener("flow-step-footer-state-changed", (event) => {
      event.stopPropagation();
      card.formLoading = Boolean(event.detail?.loading);
      this._updateNativeSubmitButton(card);
    });
    context.append(form);
    card.context = context;
    card.form = form;
    card.body.append(context);
  }

  _updateForm(card) {
    if (!card.form) return;
    card.context.hass = this._hass;
    card.form.hass = this._hass;
    card.form.step = this._nativeStep(card.record);
  }

  async _submitNativeStep(card, lifecycle, hass, interactionId, input) {
    if (lifecycle !== this.lifecycle || interactionId !== card.id || !input ||
        typeof input !== "object" || Array.isArray(input) || this.busy.has(card.id)) {
      throw this._nativeSubmitError("invalid_input");
    }
    this.busy.add(card.id);
    this._updateBusy(card);
    try {
      const result = await hass.callWS({
        type: "habab_home/setup/submit",
        interaction_id: interactionId,
        input,
        language: this._language(),
      });
      if (lifecycle !== this.lifecycle || !this.isConnected || this.cards.get(card.id) !== card) {
        throw this._nativeSubmitError("unknown_interaction");
      }
      const interaction = normalizeInteraction(result);
      if (!interaction || interaction.id !== card.id || interaction.domain !== card.record.domain ||
          interaction.kind !== card.record.kind) throw this._nativeSubmitError("setup_failed");
      card.pendingInteraction = interaction;
      return this._nativeStep(interaction);
    } catch (error) {
      if (error?.body?.errors && typeof error.body.errors === "object") {
        const allowedFields = new Set(card.record.data_schema.map((field) => field.name));
        const errors = safeErrorMap(error.body.errors, allowedFields);
        if (Object.keys(errors).length) {
          const sanitized = new Error("setup_failed");
          sanitized.body = {errors};
          throw sanitized;
        }
      }
      throw this._nativeSubmitError(this._safeCode(error));
    } finally {
      if (lifecycle === this.lifecycle) {
        this.busy.delete(card.id);
        this._updateBusy(card);
        this.schedulePoll();
      }
    }
  }

  _nativeSubmitError(code) {
    const error = new Error("setup_failed");
    error.body = {errors: {base: ERROR_PATTERN.test(code) ? code : "setup_failed"}};
    return error;
  }

  _integrationTitle(domain) {
    const key = `component.${domain}.title`;
    const translated = this._hass?.localize?.(key);
    return typeof translated === "string" && translated !== key && translated ? translated : domain;
  }

  _entryStateText(state) {
    if (typeof state !== "string" || !ERROR_PATTERN.test(state)) return undefined;
    const key = `ui.panel.config.integrations.config_entry.state.${state}`;
    const translated = this._hass?.localize?.(key);
    return typeof translated === "string" && translated !== key && translated ? translated : undefined;
  }

  _errorText(domain, code, stepId) {
    if (typeof code !== "string" || !ERROR_PATTERN.test(code)) {
      return this._localize("ui.panel.config.integrations.config_flow.error", undefined, this._copy("Не удалось настроить интеграцию.", "The integration setup failed."));
    }
    if (typeof domain === "string" && DOMAIN_PATTERN.test(domain)) {
      const key = `component.${domain}.config.error.${code}`;
      const translated = this._hass?.localize?.(key);
      if (typeof translated === "string" && translated !== key && translated) return translated;
      if (stepId && DOMAIN_PATTERN.test(stepId)) {
        const stepKey = `component.${domain}.config.step.${stepId}.error.${code}`;
        const stepTranslation = this._hass?.localize?.(stepKey);
        if (typeof stepTranslation === "string" && stepTranslation !== stepKey && stepTranslation) return stepTranslation;
      }
    }
    return this._localize("ui.panel.config.integrations.config_flow.error", undefined, this._copy("Не удалось настроить интеграцию.", "The integration setup failed."));
  }

  _text(parent, value, type = "message") {
    const node = document.createElement("p");
    node.className = type;
    node.textContent = value;
    if (type === "error") node.setAttribute("role", "alert");
    else node.setAttribute("role", "status");
    parent.append(node);
    return node;
  }

  _button(card, parent, label, action, primary = false) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    if (primary) button.className = "primary";
    button.disabled = this.busy.has(card.id);
    button.addEventListener("click", action);
    parent.append(button);
    return button;
  }

  _updateBusy(card) {
    if (!card) return;
    const disabled = this.busy.has(card.id);
    for (const control of card.element.querySelectorAll("button")) control.disabled = disabled;
    this._updateNativeSubmitButton(card);
  }

  _emitState(phase, domain) {
    if (!PHASES.has(phase) || typeof domain !== "string" || !DOMAIN_PATTERN.test(domain)) return;
    this.dispatchEvent(new CustomEvent("habab-setup-state", {
      bubbles: true, composed: true, detail: {phase, domain},
    }));
  }

  _verify(domain) {
    if (!DOMAIN_PATTERN.test(domain)) return;
    this.dispatchEvent(new CustomEvent("habab-setup-verify", {
      bubbles: true, composed: true, detail: {domain},
    }));
  }

  _disposeForm(card) {
    card.flowLoading = undefined;
    card.pendingInteraction = undefined;
    card.formLoading = false;
    card.nativeSubmitPending = false;
    card.submitButton = undefined;
    card.form?.remove();
    card.context?.remove();
    card.form = undefined;
    card.context = undefined;
    card.readyForm = undefined;
    card.flowConfig = undefined;
    card.nativeForm = undefined;
    card.formLoadError = undefined;
  }

  _disposeCard(card) {
    this._disposeForm(card);
    card.element.remove();
  }

  _applyOne(value) {
    const interaction = normalizeInteraction(value);
    if (!interaction) return undefined;
    this._applyInteraction(interaction);
    this.interactions = this.interactions.filter((item) => item.id !== interaction.id);
    this.interactions.push(interaction);
    this._render();
    return interaction;
  }

  async start(domain) {
    if (typeof domain !== "string" || !DOMAIN_PATTERN.test(domain)) return;
    return this._mutate({type: "habab_home/setup/start", domain, language: this._language()});
  }

  async adopt(flowId) {
    if (typeof flowId !== "string" || !flowId) return;
    return this._mutate({type: "habab_home/setup/adopt", flow_id: flowId, language: this._language()});
  }

  async submit(id, input) {
    if (!id || !input || typeof input !== "object" || Array.isArray(input)) return;
    return this._mutate({type: "habab_home/setup/submit", interaction_id: id, input, language: this._language()});
  }

  async cancel(id) {
    if (!id) return;
    return this._mutate({type: "habab_home/setup/cancel", interaction_id: id});
  }

  async dismiss(id) {
    if (!id) return;
    if (this.busy.has(id)) return;
    const lifecycle = this.lifecycle;
    this.busy.add(id); this._updateBusy(this.cards.get(id));
    try {
      await this._hass.callWS({type: "habab_home/setup/dismiss", interaction_id: id});
      if (lifecycle !== this.lifecycle || !this.isConnected) return;
      const card = this.cards.get(id);
      if (card) this._disposeCard(card);
      this.cards.delete(id);
      this.interactions = this.interactions.filter((item) => item.id !== id);
    } catch (error) {
      if (lifecycle === this.lifecycle && this.isConnected) {
        this.statusError = this._errorText("", this._safeCode(error));
        this._render();
      }
    } finally {
      if (lifecycle === this.lifecycle) {
        this.busy.delete(id);
        if (this.isConnected && this._active) this.schedulePoll();
      }
    }
  }

  async _mutate(message) {
    const id = message.interaction_id;
    if (id && this.busy.has(id)) return;
    const lifecycle = this.lifecycle;
    if (id) { this.busy.add(id); this._updateBusy(this.cards.get(id)); }
    try {
      const result = await this._hass.callWS(message);
      if (lifecycle !== this.lifecycle || !this.isConnected) return;
      if (result?.id) this._applyOne(result);
      else await this.refresh();
      return result;
    } catch (error) {
      if (lifecycle === this.lifecycle && this.isConnected) {
        this.statusError = this._errorText("", this._safeCode(error));
        this._render();
      }
      return undefined;
    } finally {
      if (lifecycle === this.lifecycle) {
        if (id) { this.busy.delete(id); this._updateBusy(this.cards.get(id)); }
        this.schedulePoll();
      }
    }
  }

  _shouldPoll() {
    return Boolean(this.isConnected && this._hass && this._hass.connected !== false &&
      (this._active || this.interactions.some((item) => item.phase === "progress")) && !this.busy.size);
  }

  schedulePoll() {
    clearTimeout(this.pollTimer);
    this.pollTimer = undefined;
    if (!this._shouldPoll()) return;
    this.pollTimer = setTimeout(() => this.refresh(), POLL_INTERVAL);
  }
}

if (globalThis.customElements && !customElements.get("habab-integration-setup")) {
  customElements.define("habab-integration-setup", HababIntegrationSetup);
}
