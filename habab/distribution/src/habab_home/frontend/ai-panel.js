import { strings } from "./strings.js";
import "./telemetry-panel.js";

const providers = {
  habab: { name: "Habab", model: "gemini-3.1-flash-lite", keyUrl: "https://habab.ru/" },
  openai: { name: "OpenAI", model: "gpt-4.1-mini", keyUrl: "https://platform.openai.com/api-keys" },
  deepseek: { name: "DeepSeek", model: "deepseek-flash", keyUrl: "https://platform.deepseek.com/api_keys" },
  gemini: { name: "Google Gemini", model: "gemini-3.1-flash-lite", keyUrl: "https://aistudio.google.com/api-keys" },
};

const modelCopy = {
  ru: {
    compatibilityLabel: "Сохранённое значение для совместимости",
    managed: "Habab выбирает внешнюю модель. Псевдоним маршрута: {alias} ({protocol}). Фактическая модель внешнего провайдера не сообщается.",
    managedNotReady: "Habab выбирает внешнюю модель через маршрут {alias} ({protocol}), но подключение пока не готово. Фактическая модель не сообщается.",
    pending: "Модель определит сервис при проверке подключения.",
    unavailable: "Не удалось подтвердить фактическую модель Habab. Сохранённое значение оставлено для совместимости.",
    legacy: "Текущий маршрут: {protocol}, модель {model}.",
    protocolGoogle: "Google Gemini",
    protocolOpenAI: "OpenAI-совместимый API",
    protocolUnavailable: "не подтверждён",
  },
  en: {
    compatibilityLabel: "Saved compatibility value",
    managed: "Habab selects the upstream model. Gateway alias: {alias} ({protocol}). The upstream model is not reported.",
    managedNotReady: "Habab selects the upstream model through {alias} ({protocol}), but the connection is not ready. The upstream model is not reported.",
    pending: "The service will choose the model when you check the connection.",
    unavailable: "The effective Habab model could not be confirmed. The saved value is kept for compatibility.",
    legacy: "Current route: {protocol}, model {model}.",
    protocolGoogle: "Google Gemini",
    protocolOpenAI: "OpenAI-compatible API",
    protocolUnavailable: "unconfirmed",
  },
};

export function assistantProviderPresentation(entry) {
  if (
    entry?.provider === "habab" &&
    typeof entry.managed_model === "string" &&
    entry.managed_model
  ) {
    const upstreamProvider = typeof entry.configured_upstream_provider === "string"
      ? providers[entry.configured_upstream_provider]?.name : undefined;
    const upstreamModel = typeof entry.configured_upstream_model === "string"
      ? entry.configured_upstream_model.trim() : "";
    if (entry.text_ready === true && upstreamProvider && upstreamModel) {
      return { key: "currentAssistantManagedRoute", provider: upstreamProvider, model: upstreamModel };
    }
    return { key: "currentAssistantManaged" };
  }
  const model = typeof entry?.effective_model === "string" ? entry.effective_model.trim() : "";
  return model
    ? { key: "currentAssistant", model }
    : { key: "currentAssistantModelUnavailable" };
}

export function assistantRoutingSummary(entry, pipelines, language) {
  const copy = strings[language === "ru" ? "ru" : "en"];
  const presentation = assistantProviderPresentation(entry);
  const textProvider = providers[entry?.provider]?.name || entry?.provider || "";
  const lines = [copy[presentation.key]
    .replace("{provider}", textProvider)
    .replace("{upstreamProvider}", presentation.provider || "")
    .replace("{model}", presentation.model || "")
    .replace("{alias}", presentation.alias || "")];

  if (entry?.voice_ready === true && entry.voice_provider && entry.default_voice_model) {
    const voiceCopy = entry.voice_protocol === "gemini_live"
      ? entry.voice_provider === "habab" ? copy.voiceRouteHababGemini
        : entry.voice_provider === "gemini" ? copy.voiceRouteGemini : copy.voiceRouteUnknown
      : entry.voice_protocol === "openai_realtime" && entry.voice_provider === "openai"
        ? copy.voiceRouteOpenAI : copy.voiceRouteUnknown;
    lines.push(voiceCopy.replace("{model}", entry.default_voice_model));
  } else if (entry?.voice_ready === false) {
    lines.push(copy.voiceRouteUnavailable);
  } else {
    lines.push(copy.voiceRouteUnknown);
  }

  lines.push(copy.androidWakeDetection);
  const ttsEngines = [...new Set((Array.isArray(pipelines) ? pipelines : [])
    .map((pipeline) => pipeline?.tts_engine)
    .filter((engine) => typeof engine === "string" && engine.trim()))];
  if (ttsEngines.length) {
    lines.push(copy.assistTTSConfigured.replace("{engine}", ttsEngines.join(", ")));
  }
  return lines;
}

export function providerModelPresentation(provider, entry, language) {
  if (provider !== "habab") return { readOnly: false, label: null, help: null };
  const copy = modelCopy[language === "ru" ? "ru" : "en"];
  const compatibility = { readOnly: true, label: copy.compatibilityLabel };
  if (!entry) return { readOnly: true, label: null, help: copy.pending };

  const managedModel = typeof entry.managed_model === "string" && entry.managed_model
    ? entry.managed_model : null;
  if (managedModel) {
    const protocol = entry.protocol === "google" ? copy.protocolGoogle
      : entry.protocol === "openai_compatible" ? copy.protocolOpenAI : copy.protocolUnavailable;
    const template = entry.text_ready === false ? copy.managedNotReady : copy.managed;
    return {
      ...compatibility,
      help: template.replace("{alias}", managedModel).replace("{protocol}", protocol),
    };
  }

  if (typeof entry.effective_model === "string" && entry.effective_model &&
      typeof entry.protocol === "string" && entry.protocol !== "unavailable") {
    const protocol = entry.protocol === "google" ? copy.protocolGoogle
      : entry.protocol === "openai_compatible" ? copy.protocolOpenAI : copy.protocolUnavailable;
    return {
      readOnly: false,
      label: null,
      help: copy.legacy.replace("{protocol}", protocol).replace("{model}", entry.effective_model),
    };
  }

  return { ...compatibility, help: copy.unavailable };
}

class HababAIPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.provider = "openai";
    this.providerSelectionExplicit = false;
    this.initialProviderSelectionApplied = false;
    this.connections = [];
    this.connectionsLoaded = false;
    this.connectionCheckStates = new Map();
    this.pipelines = [];
    this.keyFetchGeneration = 0;
    this.keyVisibilityPreference = null;
    this.keyVisibilityProvider = null;
    this.savedKeyLoaded = false;
    this.keyEdited = false;
    this.modelEdited = false;
    this.feedbackCredentialDraft = "";
    this.feedbackDeliveryState = { enabled: false, credential_configured: false, ready: false };
    this.feedbackDeliveryBusy = false;
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
    this.keyFetchGeneration += 1;
    this.connectionCheckStates.clear();
    this.busy = false;
    const key = this.shadowRoot.querySelector("#key");
    if (key) key.value = "";
    this.feedbackCredentialDraft = "";
    const feedbackKey = this.shadowRoot.querySelector("#feedback-delivery-token");
    if (feedbackKey) feedbackKey.value = "";
  }

  t(key) { return strings[this.language || "en"][key] || strings[this.language || "en"].unknown; }
  q(selector) { return this.shadowRoot.querySelector(selector); }
  entry() { return this.connections.find((item) => item.provider === this.provider); }
  checkStateKey(entry) { return `${entry.entry_id}:${entry.model}`; }
  checkTime(value) {
    if (typeof value !== "string") return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : new Intl.DateTimeFormat(this.language, {
      dateStyle: "medium", timeStyle: "short",
    }).format(date);
  }
  render() {
    const activeElement = this.shadowRoot.activeElement;
    const restoreFeedbackTokenFocus = activeElement?.id === "feedback-delivery-token";
    const feedbackTokenSelection = restoreFeedbackTokenFocus
      ? [activeElement.selectionStart, activeElement.selectionEnd, activeElement.selectionDirection]
      : null;
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
      .help,.status{color:var(--secondary-text-color);font-size:13px}.status{margin-block:var(--ha-space-2,8px)}.routing-summary{white-space:pre-line}ol{padding-inline-start:22px;margin:8px 0 16px}li{padding-inline-start:4px;margin-block:8px}
      form{margin-block-start:var(--ha-space-6,24px)}label{display:block;font-weight:500;margin:var(--ha-space-4,16px) 0 var(--ha-space-2,8px)}input,textarea{width:100%;min-width:0;padding:12px;border:1px solid var(--secondary-text-color);border-radius:6px;background:var(--card-background-color);color:var(--primary-text-color);caret-color:var(--primary-color)}
      input{height:48px}.key-row{display:flex;gap:8px}.key-row input{flex:1}.key-row button{display:inline-flex;flex:none;align-items:center;justify-content:center;min-width:48px;padding:8px}.key-row ha-icon{--mdc-icon-size:20px}textarea{resize:vertical;min-height:88px}input::placeholder,textarea::placeholder{color:var(--secondary-text-color);opacity:1}
      #feedback-delivery-enabled{width:auto;min-width:auto;height:auto;margin-inline-end:8px}label:has(#feedback-delivery-enabled){display:flex;align-items:center;gap:4px}
      .actions{display:flex;flex-wrap:wrap;gap:var(--ha-space-2,8px);margin-block:var(--ha-space-4,16px)}.primary{background:var(--ha-color-fill-primary-loud-hover,var(--dark-primary-color));border-color:transparent;color:var(--ha-color-on-primary-loud,var(--text-primary-color))}.primary:hover{filter:brightness(.93);background:var(--ha-color-fill-primary-loud-hover,var(--dark-primary-color))}
      #feedback{padding:var(--ha-space-4,16px);border:1px solid var(--divider-color);border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere}#feedback[data-error=true]{color:var(--error-color)}[hidden]{display:none!important}
      .assistant{padding-block-start:var(--ha-space-8,32px);border-block-start:1px solid var(--divider-color)}.links{display:flex;flex-wrap:wrap;gap:12px 24px;margin-block:16px}.disclosure{margin-block-start:32px}
      @container(max-width:650px){main{padding:24px 16px 48px}.workspace{grid-template-columns:minmax(0,1fr);gap:24px;margin-block:24px}nav{flex-direction:row;flex-wrap:wrap}nav button{flex:1 1 auto;text-align:center}.key-row{flex-wrap:wrap}.key-row input{flex:1 1 100%}h2{font-size:21px}.actions button{flex:1 1 auto}}
    </style>
    <header><ha-menu-button></ha-menu-button><h1>${this.t("title")}</h1></header>
    <main><p class="intro">${this.t("intro")}</p><div class="workspace">
      <nav aria-label="${this.t("provider")}">${Object.entries(providers).map(([id, item]) => `<button type="button" data-provider="${id}" aria-pressed="${id === this.provider}">${item.name}</button>`).join("")}</nav>
      <section aria-labelledby="provider-title"><h2 id="provider-title"></h2><p class="status" id="connection-status"></p><p class="status" id="api-status" role="status" aria-live="polite"></p>
      <a id="key-link" target="_blank" rel="noopener noreferrer">${this.t("getKey")}</a>
      <h3>${this.t("guide")}</h3><ol id="guide"></ol>
      <form><label id="model-label" for="model">${this.t("model")}</label><input id="model" name="model" required maxlength="128" autocomplete="off" aria-describedby="model-help"><p class="help" id="model-help">${this.t("modelHelp")}</p>
      <label for="key">${this.t("key")}</label><div class="key-row"><input id="key" name="api_key" type="password" required maxlength="512" autocomplete="off" spellcheck="false" autocapitalize="off" aria-describedby="key-help"><button type="button" id="reveal" aria-label="${this.t("show")}" aria-pressed="false"><ha-icon icon="mdi:eye"></ha-icon></button></div>
      <p class="help" id="key-help">${this.t("keyHelp")}</p><div class="actions"><button class="primary" type="submit">${this.t("save")}</button><button type="button" id="check">${this.t("check")}</button></div><p class="help">${this.t("cost")}</p></form>
      <div id="feedback" role="status" aria-live="polite" hidden></div></section></div>
      <section class="assistant" aria-labelledby="assistant-title"><h2 id="assistant-title">${this.t("assistantTitle")}</h2><p>${this.t("assistantHelp")}</p>
      <p class="status routing-summary" id="assistant-provider-status" role="status" aria-live="polite"></p>
      <div class="actions"><button type="button" id="open-assistant" class="primary">${this.t("openAssistant")}</button></div><p class="help status" id="assistant-status" role="status" aria-live="polite"></p>
      <div class="links"><a href="/config/voice-assistants/expose">${this.t("expose")}</a><a href="/config/voice-assistants/assistants">${this.t("pipelineSettings")}</a></div>
      <p class="help" id="pipeline-status"></p></section>
      <section class="assistant" aria-labelledby="feedback-delivery-title"><h2 id="feedback-delivery-title">${this.t("feedbackDeliveryTitle")}</h2>
      <p>${this.t("feedbackDeliveryHelp")}</p><p class="help" id="feedback-delivery-endpoint"></p>
      <p class="status" id="feedback-delivery-state" role="status" aria-live="polite"></p>
      <label for="feedback-delivery-token">${this.t("feedbackDeliveryToken")}</label>
      <input id="feedback-delivery-token" type="password" maxlength="512" autocomplete="new-password" spellcheck="false" autocapitalize="off" aria-describedby="feedback-delivery-key-help">
      <p class="help" id="feedback-delivery-key-help">${this.t("feedbackDeliveryTokenHelp")}</p>
      <div class="actions"><button type="button" id="feedback-delivery-save" class="primary">${this.t("feedbackDeliverySave")}</button>
      <button type="button" id="feedback-delivery-clear">${this.t("feedbackDeliveryClear")}</button></div>
      <label><input id="feedback-delivery-enabled" type="checkbox"> ${this.t("feedbackDeliveryEnable")}</label>
      <p class="help disclosure">${this.t("feedbackDeliveryPrivacy")}</p></section>
      <p class="help disclosure">${this.t("cloud")}</p>
      <habab-telemetry-panel></habab-telemetry-panel></main>`;
    this.q("#feedback-delivery-token").value = this.feedbackCredentialDraft;
    this.q("#feedback-delivery-endpoint").textContent = this.t("feedbackDeliveryEndpoint");
    if (restoreFeedbackTokenFocus) {
      const input = this.q("#feedback-delivery-token");
      input.focus();
      if (Number.isInteger(feedbackTokenSelection[0]) && Number.isInteger(feedbackTokenSelection[1])) {
        input.setSelectionRange(feedbackTokenSelection[0], feedbackTokenSelection[1], feedbackTokenSelection[2]);
      }
    }
    this.q("ha-menu-button").hass = this._hass;
    this.q("habab-telemetry-panel").hass = this._hass;
    this.q("nav").addEventListener("click", (event) => {
      const button = event.target.closest("[data-provider]");
      if (!button || this.busy) return;
      this.providerSelectionExplicit = true;
      this.provider = button.dataset.provider;
      this.selectProvider();
    });
    this.q("form").addEventListener("submit", (event) => { event.preventDefault(); this.save(); });
    this.q("#key").addEventListener("input", () => {
      this.keyEdited = true;
      this.keyFetchGeneration += 1;
    });
    this.q("#model").addEventListener("input", () => { this.modelEdited = true; });
    this.q("#reveal").addEventListener("click", () => {
      const showing = this.q("#key").type === "password";
      this.keyVisibilityPreference = showing ? "visible" : "hidden";
      this.applyKeyVisibility();
    });
    this.q("#check").addEventListener("click", () => this.check());
    this.q("#open-assistant").addEventListener("click", () => this.openAssistant());
    this.q("#feedback-delivery-token").addEventListener("input", (event) => {
      this.feedbackCredentialDraft = event.target.value;
      this.applyFeedbackDeliveryControls();
    });
    this.q("#feedback-delivery-save").addEventListener("click", () => this.feedbackDeliveryAction("save"));
    this.q("#feedback-delivery-clear").addEventListener("click", () => this.feedbackDeliveryAction("clear"));
    this.q("#feedback-delivery-enabled").addEventListener("change", (event) => {
      this.feedbackDeliveryAction("enable", { enabled: event.target.checked });
    });
    this.selectProvider();
  }

  selectProvider({ preserveDraft = false } = {}) {
    if (this.keyVisibilityProvider !== this.provider) {
      this.keyVisibilityPreference = null;
      this.savedKeyLoaded = false;
      this.keyVisibilityProvider = this.provider;
    }
    const provider = providers[this.provider];
    const generation = ++this.keyFetchGeneration;
    this.q("#provider-title").textContent = provider.name;
    this.q("#key-link").href = provider.keyUrl;
    this.q("#key-link").textContent = this.t(this.provider === "habab" ? "hababOpen" : "getKey");
    if (!preserveDraft || !this.modelEdited) {
      this.q("#model").value = this.entry()?.model || provider.model;
      this.modelEdited = false;
    }
    const modelPresentation = providerModelPresentation(this.provider, this.entry(), this.language);
    this.q("#model").readOnly = modelPresentation.readOnly;
    this.q("#model-label").textContent = modelPresentation.label || this.t("model");
    this.q("#model-help").textContent = modelPresentation.help || this.t("modelHelp");
    if (!preserveDraft || !this.keyEdited) {
      this.q("#key").value = "";
      this.keyEdited = false;
    }
    this.applyKeyVisibility();
    this.q("#guide").replaceChildren(...this.t(`${this.provider}Guide`).map((text) => {
      const item = document.createElement("li"); item.textContent = text; return item;
    }));
    this.shadowRoot.querySelectorAll("[data-provider]").forEach((item) => item.setAttribute("aria-pressed", String(item.dataset.provider === this.provider)));
    this.q("#feedback").hidden = true;
    this.updateControls();
    const entry = this.entry();
    if (!entry || this.keyEdited) return;
    this._hass.callWS({ type: "habab_home/providers/key", entry_id: entry.entry_id })
      .then((result) => {
        if (!this.isConnected || generation !== this.keyFetchGeneration ||
            this.provider !== entry.provider || this.entry()?.entry_id !== entry.entry_id || this.keyEdited) return;
        this.q("#key").value = result.api_key;
        this.savedKeyLoaded = true;
        this.applyKeyVisibility();
      })
      .catch((error) => {
        if (!this.isConnected || generation !== this.keyFetchGeneration ||
            this.provider !== entry.provider || this.entry()?.entry_id !== entry.entry_id) return;
        this.feedback(this.t(error.code), true);
      });
  }

  applyKeyVisibility() {
    const key = this.q("#key");
    const button = this.q("#reveal");
    const visible = this.keyVisibilityPreference === "visible" ||
      (this.keyVisibilityPreference === null && this.savedKeyLoaded);
    key.type = visible ? "text" : "password";
    button.setAttribute("aria-label", this.t(visible ? "hide" : "show"));
    button.setAttribute("aria-pressed", String(visible));
    button.querySelector("ha-icon").setAttribute("icon", visible ? "mdi:eye-off" : "mdi:eye");
  }

  async refresh() {
    const [connections, pipelines, feedbackDelivery] = await Promise.allSettled([
      this._hass.callWS({ type: "habab_home/providers/list" }),
      this._hass.callWS({ type: "assist_pipeline/pipeline/list" }),
      this._hass.callWS({ type: "habab_home/feedback_delivery/status" }),
    ]);
    if (connections.status === "fulfilled") {
      this.connections = connections.value.connections;
      this.connectionsLoaded = true;
      if (!this.initialProviderSelectionApplied) {
        this.initialProviderSelectionApplied = true;
        const selected = this.connections.filter((item) => item.assistant_selected);
        if (!this.providerSelectionExplicit && !this.keyEdited && !this.modelEdited &&
            selected.length === 1 && providers[selected[0].provider]) {
          this.provider = selected[0].provider;
        }
      }
      this.selectProvider({ preserveDraft: true });
    } else {
      this.connectionsLoaded = false;
    }
    if (pipelines.status === "fulfilled") {
      this.pipelines = pipelines.value.pipelines;
      this.q("#pipeline-status").textContent = this.pipelines.length
        ? this.t("pipelinesReady").replace("{count}", String(this.pipelines.length))
        : this.t("pipelinesMissing");
    } else {
      this.q("#pipeline-status").textContent = this.t(pipelines.reason?.code || "unknown");
    }
    if (feedbackDelivery.status === "fulfilled") {
      this.feedbackDeliveryState = feedbackDelivery.value;
      this.q("#feedback-delivery-state").textContent = this.feedbackDeliveryStatusText();
    } else {
      this.q("#feedback-delivery-state").textContent = this.t("feedbackDeliveryStatusUnavailable");
    }
    this.updateControls();
    const failure = connections.status === "rejected" ? connections.reason
      : pipelines.status === "rejected" ? pipelines.reason : null;
    if (failure) this.feedback(this.t(failure.code || "unknown"), true);
  }

  updateControls() {
    const entry = this.entry();
    const selected = this.connections.filter((item) => item.assistant_selected);
    const currentProvider = selected.length === 1 ? selected[0] : null;
    this.q("#assistant-provider-status").textContent = !this.connectionsLoaded
      ? this.t("assistantProviderUnavailable")
      : currentProvider
        ? assistantRoutingSummary(currentProvider, this.pipelines, this.language).join("\n")
        : this.t(selected.length ? "assistantProviderAmbiguous" : "noAssistantProvider");
    this.shadowRoot.querySelectorAll("button,input,textarea").forEach((item) => { item.disabled = !!this.busy; });
    this.q("#check").disabled = !!this.busy || !this.connectionsLoaded || !entry;
    if (!this.connectionsLoaded) {
      this.q("#connection-status").textContent = this.t("connectionStatusUnavailable");
      this.q("#api-status").textContent = "";
    } else if (!entry) {
      this.q("#connection-status").textContent = this.t("disconnected");
      this.q("#api-status").textContent = this.t("apiNotConfigured");
    } else {
      this.q("#connection-status").textContent = this.t(entry.assistant_selected ? "savedForAssistant" : "saved");
      const pending = this.connectionCheckStates.get(this.checkStateKey(entry)) === "checking";
      const result = pending ? { status: "checking" } : {
        status: entry.api_check_status || "unknown",
        checked_at: entry.api_check_at,
        reason: entry.api_check_reason,
      };
      const time = this.checkTime(result.checked_at);
      const state = result.status || "unknown";
      const statusKey = {
        checking: "apiChecking",
        verified: time ? "apiVerifiedAt" : "apiVerified",
        rejected: time ? "apiRejectedAt" : "apiRejected",
      }[state] || ({
        invalid_provider: time ? "apiInvalidProviderAt" : "apiInvalidProvider",
        quota: time ? "apiQuotaAt" : "apiQuota",
        invalid_model: time ? "apiInvalidModelAt" : "apiInvalidModel",
        cannot_connect: time ? "apiUnavailableAt" : "apiUnavailable",
        timeout: time ? "apiUnavailableAt" : "apiUnavailable",
        invalid_response: time ? "apiInvalidResponseAt" : "apiInvalidResponse",
        tools_unsupported: time ? "apiToolsUnsupportedAt" : "apiToolsUnsupported",
      }[result.reason] || (time ? "apiUnknownAt" : "apiUnknown"));
      const managedStatusKey = entry.managed_model
        ? pending ? "apiCheckingManaged"
          : state === "verified" ? (time ? "apiVerifiedManagedAt" : "apiVerifiedManaged")
            : result.reason === "invalid_model"
              ? (time ? "apiInvalidManagedModelAt" : "apiInvalidManagedModel")
              : null
        : null;
      this.q("#api-status").textContent = this.t(managedStatusKey || statusKey).replace("{time}", time || "");
    }
    const openAssistant = this.q("#open-assistant");
    openAssistant.disabled = !!this.busy || window.hababAssistant?.available !== true;
    this.q("#assistant-status").textContent = window.hababAssistant?.available === true
      ? "" : this.t("assistantUnavailable");
    this.applyFeedbackDeliveryControls();
  }

  feedbackDeliveryStatusText() {
    const state = this.feedbackDeliveryState;
    if (!state?.credential_configured) return this.t("feedbackDeliveryNotConfigured");
    if (!state.enabled) return this.t("feedbackDeliveryDisabled");
    return state.ready ? this.t("feedbackDeliveryReady") : this.t("feedbackDeliveryUnavailable");
  }

  applyFeedbackDeliveryControls() {
    const token = this.q("#feedback-delivery-token");
    if (!token) return;
    const state = this.feedbackDeliveryState || {};
    const busy = !!this.busy || this.feedbackDeliveryBusy;
    token.disabled = busy;
    this.q("#feedback-delivery-save").disabled = busy || !this.feedbackCredentialDraft.trim();
    this.q("#feedback-delivery-clear").disabled = busy || !state.credential_configured;
    const enabled = this.q("#feedback-delivery-enabled");
    enabled.checked = state.enabled === true;
    enabled.disabled = busy || !state.credential_configured;
  }

  async feedbackDeliveryAction(action, payload = {}) {
    if (this.feedbackDeliveryBusy || this.busy) return;
    const type = {
      save: "habab_home/feedback_delivery/save",
      clear: "habab_home/feedback_delivery/clear",
      enable: "habab_home/feedback_delivery/enable",
    }[action];
    if (!type) return;
    this.feedbackDeliveryBusy = true;
    this.applyFeedbackDeliveryControls();
    const status = this.q("#feedback-delivery-state");
    status.textContent = this.t("feedbackDeliverySaving");
    status.dataset.error = "false";
    try {
      const message = { type, ...payload };
      if (action === "save") message.token = this.feedbackCredentialDraft.trim();
      this.feedbackDeliveryState = await this._hass.callWS(message);
      if (action === "save" || action === "clear") {
        this.feedbackCredentialDraft = "";
        this.q("#feedback-delivery-token").value = "";
      }
      status.textContent = this.feedbackDeliveryStatusText();
    } catch (error) {
      const code = error?.code || "unknown";
      status.textContent = this.t({
        credential_invalid: "feedbackDeliveryCredentialInvalid",
        credential_required: "feedbackDeliveryCredentialRequired",
        feedback_unavailable: "feedbackDeliverySaveFailed",
        invalid_enable_state: "feedbackDeliverySaveFailed",
      }[code] || "feedbackDeliverySaveFailed");
      status.dataset.error = "true";
      if (action === "enable") this.q("#feedback-delivery-enabled").checked = this.feedbackDeliveryState.enabled === true;
    } finally {
      this.feedbackDeliveryBusy = false;
      this.applyFeedbackDeliveryControls();
    }
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
      const model = this.q("#model").value.trim();
      await this._hass.callWS({ type: "habab_home/providers/save", provider: this.provider,
        model, api_key: this.q("#key").value.trim() });
      this.q("#key").value = "";
      this.keyEdited = false;
      this.modelEdited = false;
      await this.refresh();
      const entry = this.entry();
      if (this.connectionsLoaded && entry?.model === model && entry.api_check_status === "verified") {
        this.feedback(this.t("verified"));
      } else {
        this.feedback(this.t("connection_changed"), true);
      }
    }, "providerSaveTimeout");
  }

  async check() {
    if (this.busy) return;
    const entry = this.entry();
    if (!entry) return;
    const checkStateKey = this.checkStateKey(entry);
    this.connectionCheckStates.set(checkStateKey, "checking");
    this.updateControls();
    await this.action(async () => {
      try {
        await this._hass.callWS({ type: "habab_home/providers/check", entry_id: entry.entry_id });
      } catch (error) {
        this.connectionCheckStates.delete(checkStateKey);
        await this.refresh();
        throw error;
      }
      this.connectionCheckStates.delete(checkStateKey);
      await this.refresh();
      const readback = this.connections.find((item) => item.entry_id === entry.entry_id);
      if (!this.connectionsLoaded || readback?.api_check_status !== "verified") {
        this.feedback(this.t("connection_changed"), true);
        return;
      }
      this.updateControls();
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
