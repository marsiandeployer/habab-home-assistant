const copy = {
  en: {
    title: "Habab Home telemetry",
    intro: "Choose what to share with the Habab team over HTTPS. This consent is separate from Home Assistant's built-in analytics setting and does not change it.",
    privacy: "Your installation ID is a random pseudonymous identifier. We never send logs, passwords, chat messages, entity states, device or person names, IP addresses, or MAC addresses.",
    installationId: "Pseudonymous installation ID",
    base: "Basic versions and system type",
    baseHelp: "Home Assistant and Habab Home versions, installation type, architecture, and Python version. Basic metrics are required for usage or statistics.",
    usage: "Integration and device model usage",
    usageHelp: "Loaded integration domain names and counts grouped by device manufacturer and model. Device names are not sent.",
    statistics: "Home Assistant counts",
    statisticsHelp: "Counts of devices, entities, automations, and users. Names and IDs are not sent.",
    diagnostics: "Exception diagnostics",
    diagnosticsHelp: "Unexpected exception type, integration name, and stack frame filenames, function names, and line numbers. No error messages, source lines, or local values are sent. Limited to 20 events per day.",
    save: "Save telemetry choices",
    loading: "Loading your choices…",
    saving: "Saving and contacting the Habab server…",
    saved: "Choices saved.",
    lastDelivery: "Last accepted message",
    noDelivery: "No telemetry has been sent.",
    noDeliveryAfterSave: "Choices saved. Nothing was sent because all categories are off.",
    usageNeedsBase: "Usage and statistics require basic versions and system type.",
    loadError: "Could not load telemetry choices. Try again later.",
    invalid_preferences: "Choose valid telemetry categories.",
    base_required: "Enable basic versions and system type to share usage or statistics.",
    network_error: "The Habab server could not be reached. Your choices are saved locally; delivery was not confirmed.",
    rate_limited: "The Habab server reached its receiving limit. Delivery was not confirmed.",
    server_rejected: "The Habab server did not accept this message.",
    invalid_response: "The Habab server response could not be verified.",
    payload_too_large: "The telemetry message exceeded its size limit and was not sent.",
    collection_error: "Telemetry could not be collected safely, so no data was sent.",
    server_unavailable: "The Habab server is temporarily unavailable. Delivery was not confirmed.",
    unknown_error: "Telemetry delivery could not be confirmed.",
  },
  ru: {
    title: "Телеметрия Habab Home",
    intro: "Выберите, чем поделиться с командой Habab по HTTPS. Это отдельное согласие: оно не меняет встроенную настройку аналитики Home Assistant.",
    privacy: "Случайный ID связывает отчёты одной установки. В отчёты не входят логи, пароли, сообщения чата, состояния сущностей, имена устройств и людей, IP- и MAC-адреса.",
    installationId: "Псевдонимный ID установки",
    base: "Основные версии и тип системы",
    baseHelp: "Версии Home Assistant и Habab Home, тип установки, архитектура и версия Python. Для статистики и использования нужно включить этот пункт.",
    usage: "Использование интеграций и моделей устройств",
    usageHelp: "Домены загруженных интеграций и количество устройств по производителю и модели. Имена устройств не отправляются.",
    statistics: "Количество объектов Home Assistant",
    statisticsHelp: "Количество устройств, сущностей, автоматизаций и пользователей. Имена и ID не отправляются.",
    diagnostics: "Диагностика исключений",
    diagnosticsHelp: "Тип исключения, название интеграции и имена файлов, функций и номера строк в стеке. Текст ошибки, исходный код и локальные значения не отправляются. Не более 20 событий в день.",
    save: "Сохранить выбор телеметрии",
    loading: "Загружаем ваш выбор…",
    saving: "Сохраняем и связываемся с сервером Habab…",
    saved: "Выбор сохранён.",
    lastDelivery: "Последнее подтверждённое сообщение",
    noDelivery: "Телеметрия ещё не отправлялась.",
    noDeliveryAfterSave: "Выбор сохранён. Ничего не отправлено: все категории выключены.",
    usageNeedsBase: "Для передачи использования и статистики включите основные версии и тип системы.",
    loadError: "Не удалось загрузить настройки телеметрии. Попробуйте позже.",
    invalid_preferences: "Выберите допустимые категории телеметрии.",
    base_required: "Чтобы передавать статистику или использование, включите основные версии и тип системы.",
    network_error: "Не удалось связаться с сервером Habab. Выбор сохранён локально, доставка не подтверждена.",
    rate_limited: "Сервер Habab временно ограничил приём. Доставка не подтверждена.",
    server_rejected: "Сервер Habab не принял сообщение.",
    invalid_response: "Не удалось проверить ответ сервера Habab.",
    payload_too_large: "Сообщение телеметрии превысило допустимый размер и не отправлено.",
    collection_error: "Не удалось безопасно собрать телеметрию, поэтому данные не отправлены.",
    server_unavailable: "Сервер Habab временно недоступен. Доставка не подтверждена.",
    unknown_error: "Не удалось подтвердить доставку телеметрии.",
  },
};

const preferenceKeys = ["base", "usage", "statistics", "diagnostics"];

class HababTelemetryPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.preferences = Object.fromEntries(preferenceKeys.map((key) => [key, false]));
    this.loaded = false;
    this.busy = false;
  }

  set hass(value) {
    this._hass = value;
    const language = value.locale.language.startsWith("ru") ? "ru" : "en";
    if (language !== this.language) {
      this.language = language;
      this.render();
      if (this.loaded) this.updateStatus();
    }
    if (!this.loaded && !this.loading) this.load();
  }

  t(key) {
    return copy[this.language || "en"][key] || copy[this.language || "en"].unknown_error;
  }

  q(selector) {
    return this.shadowRoot.querySelector(selector);
  }

  render() {
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;color:var(--primary-text-color);font:14px/1.55 var(--paper-font-body1_-_font-family,Roboto,system-ui,sans-serif)}
      *{box-sizing:border-box}section{max-width:940px;margin:0 auto 40px;padding:24px;background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:12px}
      h2{font-size:20px;font-weight:500;margin:0 0 8px}p{max-width:80ch;margin:0 0 12px;color:var(--secondary-text-color)}
      .privacy{padding:12px;border-inline-start:3px solid var(--primary-color);background:var(--secondary-background-color)}
      fieldset{border:0;padding:0;margin:20px 0}label{display:grid;grid-template-columns:24px minmax(0,1fr);gap:10px;align-items:start;margin:18px 0;font-weight:500}
      input{width:20px;height:20px;margin:2px 0 0;accent-color:var(--primary-color)}input:focus-visible,button:focus-visible{outline:3px solid var(--ha-color-focus,var(--primary-color));outline-offset:3px}
      .help{display:block;color:var(--secondary-text-color);font-size:13px;font-weight:400;margin-top:3px}
      button{font:inherit;cursor:pointer;min-height:44px;padding:10px 16px;border:1px solid var(--divider-color);border-radius:8px;color:var(--primary-text-color);background:var(--card-background-color)}
      button.primary{background:var(--ha-color-fill-primary-loud-hover,var(--dark-primary-color));border-color:transparent;color:var(--ha-color-on-primary-loud,var(--text-primary-color))}button:disabled{opacity:.55;cursor:default}
      #status{margin:12px 0 0;overflow-wrap:anywhere}#status[data-error=true]{color:var(--error-color)}[hidden]{display:none!important}
      @media(max-width:600px){section{padding:18px;margin-inline:12px}}
    </style>
    <section aria-labelledby="telemetry-title">
      <h2 id="telemetry-title">${this.t("title")}</h2>
      <p>${this.t("intro")}</p><p class="privacy">${this.t("privacy")}</p>
      <p class="help">${this.t("installationId")}: <code id="installation-id"></code></p>
      <fieldset id="choices" disabled>
        ${preferenceKeys.map((key) => `<label><input type="checkbox" data-preference="${key}"><span>${this.t(key)}<span class="help">${this.t(`${key}Help`)}</span></span></label>`).join("")}
      </fieldset>
      <p class="help">${this.t("usageNeedsBase")}</p>
      <button class="primary" id="save" type="button" disabled>${this.t("save")}</button>
      <p id="status" role="status" aria-live="polite">${this.t("loading")}</p>
    </section>`;
    this.q("#choices").addEventListener("change", (event) => {
      if (event.target.dataset.preference === "base" && !event.target.checked) {
        this.shadowRoot.querySelectorAll('[data-preference="usage"],[data-preference="statistics"]').forEach((input) => {
          input.checked = false;
        });
      }
      this.updateControls();
    });
    this.q("#save").addEventListener("click", () => this.save());
    this.applyState();
  }

  async load() {
    this.loading = true;
    try {
      const state = await this._hass.callWS({ type: "habab_home/telemetry/get" });
      if (!/^[a-f0-9]{32}$/.test(state.installation_id)
          || !state.preferences
          || preferenceKeys.some((key) => typeof state.preferences[key] !== "boolean")) {
        throw new Error("invalid_response");
      }
      this.preferences = Object.fromEntries(preferenceKeys.map((key) => [key, state.preferences[key]]));
      this.installationId = state.installation_id;
      this.lastDelivery = state.last_delivery;
      this.lastError = state.last_error;
      this.loaded = true;
      this.applyState();
      this.updateStatus();
    } catch (error) {
      this.loaded = false;
      this.q("#status").textContent = this.t("loadError");
      this.q("#status").dataset.error = "true";
    } finally {
      this.loading = false;
    }
  }

  applyState() {
    if (!this.shadowRoot.firstChild) return;
    this.shadowRoot.querySelectorAll("[data-preference]").forEach((input) => {
      input.checked = this.preferences[input.dataset.preference] === true;
    });
    this.q("#installation-id").textContent = this.installationId || "";
    this.updateControls();
  }

  updateControls() {
    if (!this.shadowRoot.firstChild) return;
    const base = this.q('[data-preference="base"]');
    this.q('[data-preference="usage"]').disabled = this.busy || !base.checked || !this.loaded;
    this.q('[data-preference="statistics"]').disabled = this.busy || !base.checked || !this.loaded;
    this.q('[data-preference="base"]').disabled = this.busy || !this.loaded;
    this.q('[data-preference="diagnostics"]').disabled = this.busy || !this.loaded;
    this.q("#choices").disabled = !this.loaded;
    this.q("#save").disabled = this.busy || !this.loaded;
  }

  updateStatus() {
    const status = this.q("#status");
    if (this.lastError && copy[this.language][this.lastError]) {
      status.textContent = this.t(this.lastError);
      status.dataset.error = "true";
      return;
    }
    status.dataset.error = "false";
    const text = this.lastDelivery
      ? `${this.t("lastDelivery")}: ${this.lastDelivery}`
      : this.t("noDelivery");
    status.textContent = text;
  }

  async save() {
    if (!this.loaded || this.busy) return;
    const preferences = Object.fromEntries(preferenceKeys.map((key) => [
      key, this.q(`[data-preference="${key}"]`).checked,
    ]));
    this.busy = true;
    this.updateControls();
    const status = this.q("#status");
    status.textContent = this.t("saving");
    status.dataset.error = "false";
    try {
      const state = await this._hass.callWS({ type: "habab_home/telemetry/set", preferences });
      this.preferences = state.preferences;
      this.lastDelivery = state.last_delivery;
      this.lastError = state.last_error;
      this.applyState();
      if (this.lastError && copy[this.language][this.lastError]) {
        status.textContent = this.t(this.lastError);
        status.dataset.error = "true";
      } else if (preferenceKeys.every((key) => !preferences[key]) && !this.lastDelivery) {
        status.textContent = this.t("noDeliveryAfterSave");
      } else {
        this.updateStatus();
        if (!this.lastError && this.lastDelivery) status.textContent = `${this.t("saved")} ${status.textContent}`;
      }
    } catch (error) {
      const code = error.code && copy[this.language][error.code] ? error.code : "unknown_error";
      status.textContent = this.t(code);
      status.dataset.error = "true";
    } finally {
      this.busy = false;
      this.updateControls();
    }
  }
}

if (!customElements.get("habab-telemetry-panel")) {
  customElements.define("habab-telemetry-panel", HababTelemetryPanel);
}
