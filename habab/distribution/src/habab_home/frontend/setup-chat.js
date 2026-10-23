// Independent downstream extension: no dependency on compiled HA internals.
export function visibleTextMessages(messages) {
  if (!Array.isArray(messages)) return [];
  return messages.filter((message) =>
    typeof message?.text === "string" && message.text.trim().length > 0
  );
}

export function isOnboardingPreview(demoEnabled, previewMarker) {
  return demoEnabled === true && previewMarker === "onboarding";
}

export function appendQuickReplyToDraft(draft, prompt) {
  return typeof draft === "string" && draft.trim()
    ? `${draft.replace(/\s+$/, "")}\n${prompt}`
    : prompt;
}

export function demoChatRequestBody(text, onboarding = false) {
  return JSON.stringify(onboarding ? {text, mode: "onboarding"} : {text});
}

export function buildPreviewRows(states, language = "ru") {
  if (!states || typeof states !== "object" || Array.isArray(states)) return [];
  const labels = language === "ru" ? {
    "light.living_room_floor_lamp": "Напольная лампа",
    "light.living_room_spotlights": "Точечный свет",
    "climate.living_room": "Климат в гостиной",
  } : {
    "light.living_room_floor_lamp": "Living room floor lamp",
    "light.living_room_spotlights": "Living room spotlights",
    "climate.living_room": "Living room climate",
  };
  return Object.entries(labels).flatMap(([entityId, label]) => {
    const entry = states[entityId];
    if (!entry || typeof entry !== "object" || typeof entry.state !== "string") return [];
    return [{entityId, label, state: entry.state}];
  });
}

export function mountStableElement(parent, element) {
  if (parent && element && element.parentNode !== parent) parent.append(element);
}

export function integrationVerificationPrompt(detail, language) {
  const domain = detail?.domain;
  if (typeof domain !== "string" || !/^[a-z0-9_]{1,80}$/.test(domain)) return undefined;
  return language === "ru"
    ? `Проверь подключение интеграции ${domain} и доступные устройства через Home Assistant. Не управляй устройствами.`
    : `Check the ${domain} integration and available devices through Home Assistant. Do not control devices.`;
}

export function chooseAssistPipeline(requestedId, pipelines, preferredId, lastUsedId) {
  if (!Array.isArray(pipelines)) return undefined;
  let id = requestedId === "preferred" ? preferredId
    : requestedId === "last_used" || !requestedId ? lastUsedId || preferredId
      : requestedId;
  let selected = pipelines.find((pipeline) => pipeline?.id === id);
  if (!selected && preferredId) selected = pipelines.find((pipeline) => pipeline?.id === preferredId);
  return selected;
}

export function assistantDiagnosticMessage(diagnostic, copy) {
  if (!diagnostic || typeof diagnostic !== "object") return "";
  const flags = diagnostic.flags && typeof diagnostic.flags === "object" ? diagnostic.flags : diagnostic;
  if (flags.invalid_model || diagnostic.name === "InvalidModelError" || diagnostic.name === "ModelNotFoundError") return copy.diagnosticInvalidModel;
  if (flags.quota || diagnostic.http_status === 429 || diagnostic.name === "RateLimitError") return copy.diagnosticQuota;
  if ([401, 403].includes(diagnostic.http_status) || ["AuthenticationError", "ProviderAuthError"].includes(diagnostic.name)) return copy.diagnosticAuth;
  if (flags.mentions_context_limit || diagnostic.name === "ContextOverflowError") return copy.diagnosticContext;
  if (flags.mentions_timeout || diagnostic.name === "TimeoutError") return copy.diagnosticTimeout;
  if (flags.missing_reasoning_content || flags.mentions_reasoning_content) return copy.diagnosticReasoning;
  if (flags.mentions_tool_calls) return copy.diagnosticTools;
  return copy.diagnosticGeneric;
}

function installChat() {
  // HA installs its scoped registry polyfill before defining home-assistant.
  // Registering earlier loses the extension when that registry is replaced.
  if (customElements.get("habab-setup-chat")) return;
  const copy = {
    ru: {
      title: "Помощник", open: "Открыть чат с помощником", close: "Закрыть", fresh: "Новый чат",
      intro: "Что подключим к вашему дому?", help: "Помогу найти устройство, проверить подключение и настроить Home Assistant.",
      previewQuestion: "Что подключим к дому? Какое оборудование у вас уже есть?",
      previewDashboard: "Дом сейчас", dashboardLoading: "Загружаю состояние дома…", dashboardFailed: "Не удалось загрузить состояние дома. Проверьте подключение и повторите попытку.", dashboardRetry: "Повторить загрузку", dashboardEmpty: "Состояние устройств пока недоступно.",
      quickXiaomi: "Xiaomi Home", quickZigbee: "Zigbee", quickOther: "Другое оборудование",
      previewPlaceholder: "Например: хочу подключить пылесос Xiaomi",
      stateOn: "Включено", stateOff: "Выключено", stateHeat: "Обогрев", stateUnavailable: "Нет данных",
      placeholder: "Например: проверь Bluetooth и найди устройства", send: "Отправить", stop: "Остановить",
      working: "Проверяю и выполняю…", loading: "Подключаю помощника…", you: "Вы", assistant: "Помощник",
      demo: "DeepSeek помогает с настройкой. Реальные устройства подключаются в вашей домашней установке Home Assistant.",
      answering: "Готовлю ответ…", rate_limited: "Слишком много запросов. Повторите позже.",
      settings: "Подключить DeepSeek", retry: "Повторить подключение",
      setup_ui_unavailable: "Не удалось открыть настройку устройств. Обновите страницу и попробуйте снова.",
      deepseek_required: "Сначала подключите DeepSeek в разделе AI.",
      not_installed: "OpenCode ещё не подключён к контейнеру. Добавьте compose.opencode.yaml по инструкции установки.",
      binary_changed: "Файл OpenCode изменился. Повторите prepare-opencode.py и пересоздайте контейнер.",
      busy: "Помощник уже выполняет задачу. Дождитесь завершения и повторите подключение.",
      stopped: "Выполнение остановлено. Уже сделанные изменения могли сохраниться.",
      failed: "Не удалось завершить запрос. Некоторые действия могли выполниться. Повторите подключение, чтобы проверить историю.",
      openingDevices: "Готовлю страницу устройств…", devicesReady: "Страница «Мои устройства» открыта.",
      noVacuumDevices: "Страница открыта. Подключённые роботы-пылесосы появятся здесь, когда Home Assistant добавит их в реестр устройств.",
      devicesFailed: "Не удалось подготовить страницу устройств. Проверьте права администратора и конфигурацию Lovelace.",
      diagnosticInvalidModel: " Проверьте имя модели в настройках провайдера.",
      diagnosticQuota: " Проверьте баланс и лимиты запросов у провайдера.",
      diagnosticAuth: " Проверьте API-ключ и доступ к модели в настройках провайдера.",
      diagnosticContext: " Сократите запрос или начните новый чат.",
      diagnosticTimeout: " Проверьте состояние дома перед повтором команды.",
      diagnosticReasoning: " Проверьте, что выбранная модель совместима с OpenCode.",
      diagnosticTools: " Выберите модель с поддержкой инструментов.",
      diagnosticGeneric: " Проверьте подключение и настройки провайдера.",
      noAssistPipeline: "Голосовой помощник недоступен. Настройте его в Home Assistant и повторите попытку.",
      speechRequired: "Выбранный голосовой помощник не настроен для распознавания речи. Выберите другой в настройках голосовых помощников.",
      transcriptEmpty: "Не удалось распознать речь. Попробуйте ещё раз или введите запрос.",
      voiceReplyFailed: "Не удалось воспроизвести голосовой ответ. Ответ остался в чате.",
      mic: "Сказать запрос помощнику", stopVoice: "Завершить запись", listening: "Слушаю. Завершу запись после паузы в речи.",
      speechStart: "Слышу речь…", speechEnd: "Пауза определена. Распознаю речь…", speechNotFound: "Голосовой помощник недоступен.",
      secure_context: "Микрофону нужен HTTPS или localhost. Откройте Home Assistant через защищённое соединение.",
      microphone_denied: "Доступ к микрофону запрещён. Разрешите его в настройках браузера.",
      microphone_failed: "Не удалось включить микрофон. Проверьте устройство и повторите.",
      timeout: "Время ожидания истекло. Проверьте состояние дома перед повтором команды.",
    },
    en: {
      title: "Assistant", open: "Open assistant chat", close: "Close", fresh: "New chat",
      intro: "What shall we connect?", help: "Find devices, check connections and set up Home Assistant.",
      previewQuestion: "What should we connect to your home? What equipment do you already have?",
      previewDashboard: "Home right now", dashboardLoading: "Loading home state…", dashboardFailed: "Could not load the home state. Check your connection and try again.", dashboardRetry: "Retry loading", dashboardEmpty: "Device state is not available yet.",
      quickXiaomi: "Xiaomi Home", quickZigbee: "Zigbee", quickOther: "Other equipment",
      previewPlaceholder: "For example: I want to connect a Xiaomi vacuum",
      stateOn: "On", stateOff: "Off", stateHeat: "Heating", stateUnavailable: "Unavailable",
      placeholder: "For example: check Bluetooth and scan for devices", send: "Send", stop: "Stop",
      working: "Checking and working…", loading: "Connecting…", you: "You", assistant: "Assistant",
      demo: "DeepSeek can help with setup. Connect real devices in your own Home Assistant installation.",
      answering: "Preparing an answer…", rate_limited: "Too many requests. Try again later.",
      settings: "Connect DeepSeek", retry: "Reconnect",
      setup_ui_unavailable: "Device setup could not open. Refresh the page and try again.",
      deepseek_required: "Connect DeepSeek in AI settings first.",
      not_installed: "OpenCode is not mounted. Follow the installation guide to add compose.opencode.yaml.",
      binary_changed: "The OpenCode executable changed. Run prepare-opencode.py again and recreate the container.",
      busy: "The assistant is already working. Wait for it to finish, then reconnect.",
      stopped: "Stopped. Changes already made may remain.",
      failed: "The request did not finish. Some actions may have run. Reconnect to check the history.",
      openingDevices: "Preparing your devices page…", devicesReady: "The My devices page is open.",
      noVacuumDevices: "The page is open. Vacuums will appear when Home Assistant adds them to the device registry.",
      devicesFailed: "Could not prepare the devices page. Check administrator access and the Lovelace configuration.",
      diagnosticInvalidModel: " Check the model name in provider settings.",
      diagnosticQuota: " Check your provider balance and request limits.",
      diagnosticAuth: " Check the API key and model access in provider settings.",
      diagnosticContext: " Shorten the request or start a new chat.",
      diagnosticTimeout: " Check the home state before repeating the command.",
      diagnosticReasoning: " Check that the selected model works with OpenCode.",
      diagnosticTools: " Choose a model that supports tools.",
      diagnosticGeneric: " Check your connection and provider settings.",
      noAssistPipeline: "A voice assistant is unavailable. Configure one in Home Assistant and try again.",
      speechRequired: "The selected voice assistant is not configured for speech recognition. Choose another in Voice assistant settings.",
      transcriptEmpty: "Speech could not be recognized. Try again or enter a request.",
      voiceReplyFailed: "The spoken reply could not play. The response is still in chat.",
      mic: "Send a voice request to the assistant", stopVoice: "Finish recording", listening: "Listening. Recording ends after a pause in speech.",
      speechStart: "Speech detected…", speechEnd: "Pause detected. Transcribing…", speechNotFound: "A voice assistant is unavailable.",
      secure_context: "Microphone access requires HTTPS or localhost. Open Home Assistant using a secure connection.",
      microphone_denied: "Microphone access was denied. Allow access in your browser settings.",
      microphone_failed: "The microphone could not start. Check your device and try again.",
      timeout: "The request timed out. Check the home state before repeating the command.",
    },
  };

  class HababSetupChat extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({mode: "open"});
      this.messages = [];
      this.busy = false;
      this.voiceBusy = false;
      this.voiceGeneration = 0;
      this.previewState = {status: "idle"};
      this.previewOpened = false;
      this.previewHistoryPositioned = false;
      this.previewFocusComposer = true;
    }

    get hass() { return this._hass; }
    set hass(value) {
      const nextLanguage = this.demo || value?.locale?.language?.startsWith("ru") ? "ru" : "en";
      const languageChanged = this.language !== nextLanguage;
      this._hass = value;
      if (languageChanged) {
        this.language = nextLanguage;
        this.updateLanguage();
      }
      if (this.setupCard) this.setupCard.hass = value;
      if (this.integrationCard) this.integrationCard.hass = value;
    }

    get t() { return copy[this.language || "en"]; }
    q(selector) { return this.shadowRoot.querySelector(selector); }

    async loadSetup(afterTurn = false) {
      if (this.demo) return;
      try {
        await import("/habab-home/xiaomi-setup.js?v=2");
        await import("/habab-home/integration-setup.js?v=1");
        if (!this.setupCard) {
          this.setupCard = document.createElement("habab-xiaomi-setup");
          this.setupCard.addEventListener("habab-setup-verify", () => {
            if (this.busy) return;
            this.q("textarea").value = this.language === "ru"
              ? "Проверь подключение Xiaomi Home и доступные устройства реальными командами. Не управляй устройствами."
              : "Check Xiaomi Home and available devices using actual commands. Do not control devices.";
            this.send();
          });
          this.setupCard.addEventListener("habab-setup-state", ({detail}) => {
            if (detail.phase === "restarting") {
              this.setupRestarting = true;
              this.controls();
            } else if (this.setupRestarting) {
              this.setupRestarting = false;
              this.controls();
            }
          });
          this.setupCard.addEventListener("habab-open-devices", async () => {
            this.status(this.t.openingDevices);
            try {
              const {openDevices} = await import("/habab-home/device-dashboard.js");
              const result = await openDevices(this.hass);
              this.status(result.count ? this.t.devicesReady : this.t.noVacuumDevices);
              this.q("dialog").close();
            } catch (_) {
              this.status(this.t.devicesFailed, true);
            }
          });
        }
        if (!this.integrationCard) {
          this.integrationCard = document.createElement("habab-integration-setup");
          this.integrationCard.addEventListener("habab-setup-verify", ({detail}) => {
            const prompt = integrationVerificationPrompt(detail, this.language);
            if (this.busy || !prompt) return;
            this.q("textarea").value = prompt;
            this.send();
          });
        }
        this.setupCard.hass = this.hass;
        mountStableElement(this.q("#setup-cards"), this.setupCard);
        await this.setupCard.load();
        this.integrationCard.hass = this.hass;
        this.integrationCard.active = this.q("dialog").open;
        mountStableElement(this.q("#setup-cards"), this.integrationCard);
        if (afterTurn && typeof this.integrationCard.refresh === "function") await this.integrationCard.refresh();
        else await this.integrationCard.load();
        this.setupCard.scrollIntoView({block: "nearest"});
      } catch (_) { this.status(this.t.setup_ui_unavailable, true); }
    }

    connectedCallback() {
      if (this.shadowRoot.firstChild) {
        if (this.preview) this.installPreviewLifecycle();
        return;
      }
      this.language = this.demo || this.hass?.locale.language.startsWith("ru") ? "ru" : "en";
      const t = this.t;
      this.shadowRoot.innerHTML = `<style>
        :host{position:fixed;z-index:10;inset-inline-end:max(16px,env(safe-area-inset-right));bottom:max(16px,env(safe-area-inset-bottom));display:block;white-space:normal;pointer-events:none;color:var(--primary-text-color,#212121);font:14px/1.55 var(--paper-font-body1_-_font-family,Roboto,system-ui,sans-serif)}
        *{box-sizing:border-box}button,textarea{font:inherit}button{cursor:pointer;color:inherit;background:transparent;border:0;min-height:44px;border-radius:8px;padding:8px 12px}button:hover{background:var(--secondary-background-color,#f0f2f4)}button:disabled{opacity:.5;cursor:default}
        :is(button,textarea,a):focus-visible{outline:3px solid var(--primary-color,#007a9e);outline-offset:2px}::selection{background:var(--primary-color,#007a9e);color:#fff}
        .icon{width:56px;height:56px;padding:16px;display:grid;place-items:center}.launcher{position:relative;z-index:1;pointer-events:auto;border-radius:50%;background:var(--primary-color,#007a9e);color:var(--text-primary-color,#fff);box-shadow:0 3px 12px #0003}.launcher:hover{background:var(--dark-primary-color,#006686)}svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
        dialog{pointer-events:auto;position:fixed;inset:auto max(16px,env(safe-area-inset-right)) max(84px,calc(env(safe-area-inset-bottom) + 72px)) auto;color:var(--primary-text-color,#212121);background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#e3e6e8);border-radius:16px;padding:0;width:min(420px,calc(100vw - 32px));height:min(680px,calc(100dvh - 120px));max-width:none;max-height:none;margin:0;box-shadow:0 12px 36px #0003}
        dialog::backdrop{background:transparent}.layout{height:100%;display:flex;flex-direction:column;min-width:0}header{display:flex;align-items:center;gap:8px;padding:12px 16px;border-block-end:1px solid var(--divider-color,#e3e6e8)}h2{font-size:18px;font-weight:500;margin:0;flex:1}.fresh{font-size:13px}
        #log{flex:1;min-height:0;overflow:auto;padding:24px;scrollbar-color:var(--secondary-text-color,#6b7279) transparent;scrollbar-width:thin}.empty{height:100%;width:100%;display:flex;flex-direction:column;justify-content:center;max-width:440px;margin:auto}.empty h3{font-size:26px;line-height:1.25;font-weight:500;margin:0 0 12px}.empty p{color:var(--secondary-text-color,#5f6368);margin:0;max-width:55ch}
        .message{white-space:pre-wrap;overflow-wrap:anywhere;max-width:65ch;margin-block-end:24px}.message.user{margin-inline-start:auto;background:var(--secondary-background-color,#f0f2f4);border-radius:16px;padding:12px 16px;width:fit-content;max-width:85%}.message.assistant{padding-inline:4px}.name{font-size:12px;font-weight:500;color:var(--secondary-text-color,#5f6368);margin-block-end:5px}.message p{margin:0}ha-markdown{display:block;white-space:normal;overflow:auto;max-width:100%}
        footer{padding:12px 20px 20px}#status{font-size:13px;color:var(--secondary-text-color,#5f6368);margin:0 0 10px;white-space:pre-wrap;overflow-wrap:anywhere}#status.error{color:var(--error-color,#b3261e)}.recovery{display:flex;gap:12px;align-items:center;margin-block-end:12px}a{color:var(--primary-color,#007a9e);text-underline-offset:3px}.recovery button{text-decoration:underline}
        form{border:1px solid var(--secondary-text-color,#81868b);border-radius:14px;padding:8px;display:flex;gap:8px;align-items:flex-end}textarea{color:inherit;caret-color:var(--primary-color,#007a9e);background:transparent;border:0;resize:none;min-width:0;flex:1;min-height:64px;max-height:128px;padding:8px;line-height:1.5}textarea::placeholder{color:var(--secondary-text-color,#5f6368);opacity:1}textarea:focus-visible{outline-offset:0}.send{color:var(--text-primary-color,#fff);background:var(--primary-color,#007a9e)}.send:hover{background:var(--dark-primary-color,#006686)}[hidden]{display:none!important}
        .preview-message{max-width:none;padding:0 0 20px}.preview-message h3{font-size:23px;line-height:1.3;font-weight:500;margin:0 0 16px}.preview-dashboard{margin:0 0 18px;padding:14px 0;border-block:1px solid var(--divider-color,#e3e6e8)}.preview-dashboard h4{font-size:14px;font-weight:600;margin:0 0 10px}.preview-rows{display:grid;gap:4px}.preview-row{display:flex;justify-content:space-between;gap:12px;padding:7px 0}.preview-label{color:var(--secondary-text-color,#5f6368)}.preview-value{font-weight:500;text-align:end}.preview-state{color:var(--secondary-text-color,#5f6368);margin:0}.preview-state.error{color:var(--error-color,#b3261e)}.preview-retry{padding-inline:0;text-decoration:underline;color:var(--primary-color,#007a9e)}.quick-replies{display:flex;flex-wrap:wrap;gap:8px;margin-block-start:16px}.quick-reply{border:1px solid var(--divider-color,#c8cccf);border-radius:18px;padding:7px 12px;min-height:40px;background:var(--card-background-color,#fff)}.quick-reply:hover{border-color:var(--primary-color,#007a9e);background:var(--secondary-background-color,#f0f2f4)}
        :host([data-preview="onboarding"]){inset:0;z-index:10;pointer-events:none}:host([data-preview="onboarding"]:not([data-preview-open])) .launcher{display:grid;position:fixed;inset-inline-end:max(16px,env(safe-area-inset-right));bottom:max(16px,env(safe-area-inset-bottom));pointer-events:auto}:host([data-preview="onboarding"][data-preview-open]) .launcher{display:none}:host([data-preview="onboarding"]) dialog{inset:0 0 0 auto;width:min(440px,46vw);height:100dvh;border-radius:0;border-block:0;border-inline-end:0;box-shadow:-8px 0 28px #0002}:host([data-preview="onboarding"]) dialog:not([open]){display:none}:host([data-preview="onboarding"]) dialog::backdrop{background:transparent}:host([data-preview="onboarding"]) #log{padding:24px 22px}:host([data-preview="onboarding"]) footer{padding:12px 18px max(16px,env(safe-area-inset-bottom))}
        @media(max-width:600px){:host{inset:0;z-index:1000}.launcher{position:fixed;inset-inline-end:max(16px,env(safe-area-inset-right));bottom:max(16px,env(safe-area-inset-bottom))}dialog{inset:0;width:100vw;height:100dvh;border-radius:0;margin:0}header{padding:8px 12px}#log{padding:20px 16px}footer{padding:12px 12px max(16px,env(safe-area-inset-bottom))}.empty h3{font-size:24px}.fresh{padding-inline:8px}.message.user{max-width:92%}}
        @media(max-width:720px){:host([data-preview="onboarding"]){z-index:1000}:host([data-preview="onboarding"]) dialog{inset-inline:0;inset-block-start:var(--preview-viewport-offset-top,0px);inset-block-end:auto;width:100vw;height:var(--preview-viewport-height,100dvh);max-height:var(--preview-viewport-height,100dvh);border-radius:0}:host([data-preview="onboarding"]) .launcher{position:fixed}}
        @media(prefers-reduced-motion:no-preference){dialog[open]{animation:arrive 160ms cubic-bezier(.16,1,.3,1)}@keyframes arrive{from{transform:translateY(10px);opacity:.7}to{transform:translateY(0);opacity:1}}}
      </style>
      <button class="icon launcher" type="button" title="${t.open}" aria-label="${t.open}" aria-haspopup="dialog"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11.5a7.5 7.5 0 0 1-7.5 7.5H5l-3 3V11.5A7.5 7.5 0 0 1 9.5 4h3A7.5 7.5 0 0 1 20 11.5Z"/><path d="M7 10h8M7 14h5"/></svg></button>
      <dialog aria-labelledby="chat-title"><div class="layout"><header><h2 id="chat-title">${t.title}</h2><button class="mic icon" type="button" aria-label="${t.mic}" title="${t.mic}"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3m-4 0h8"/></svg></button><button class="fresh" type="button">${t.fresh}</button><button class="icon close" type="button" aria-label="${t.close}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button></header>
      <div id="log"><div id="messages" role="log" aria-live="polite" aria-label="${t.title}"></div><div id="setup-cards"></div></div><footer><p id="status" role="status"></p><div class="recovery" hidden><a href="/habab-ai">${t.settings}</a><button type="button" class="retry">${t.retry}</button></div>
      <form><textarea aria-label="${t.placeholder}" placeholder="${t.placeholder}" maxlength="4000" rows="2"></textarea><button class="send icon" type="submit" aria-label="${t.send}" title="${t.send}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg></button><button class="stop" type="button" hidden>${t.stop}</button></form></footer></div></dialog>`;
      this.q(".launcher").onclick = () => this.open();
      this.q(".close").onclick = () => this.q("dialog").close();
      this.q("dialog").addEventListener("close", () => {
        this.removeAttribute("data-preview-open");
        this.onClose();
      });
      this.q(".mic").onclick = () => this.voiceBusy ? this.voice?.finishRecording() : this.startVoice("last_used");
      this.q(".fresh").onclick = () => this.connect(true);
      this.q(".retry").onclick = () => this.connect();
      this.q("form").onsubmit = (event) => {event.preventDefault(); this.send();};
      this.q("textarea").onkeydown = (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {event.preventDefault(); this.send();}
      };
      this.q(".stop").onclick = () => this.stop();
      if (this.preview) {
        this.q("textarea").placeholder = t.previewPlaceholder;
        this.q("textarea").setAttribute("aria-label", t.previewPlaceholder);
      }
      this.shadowRoot.addEventListener("click", (event) => {
        const target = event.composedPath().find((item) => item?.classList);
        if (target?.classList.contains("quick-reply")) this.chooseQuickReply(target.dataset.prompt);
        if (target?.classList.contains("preview-retry")) void this.loadPreviewDashboard();
      });
      if (this.preview) this.installPreviewLifecycle();
      this.draw();
      if (this.preview && !this.previewOpened) {
        this.previewOpened = true;
        void this.loadPreviewDashboard();
        this.open({}, null, {focus: false});
      }
    }

    installPreviewLifecycle() {
      if (!this.preview || this.previewViewport) return;
      this.previewViewport = window.visualViewport;
      this.onPreviewViewportResize = () => this.updatePreviewViewport();
      this.previewViewport?.addEventListener("resize", this.onPreviewViewportResize);
      this.previewViewport?.addEventListener("scroll", this.onPreviewViewportResize);
      this.updatePreviewViewport();
    }

    disconnectedCallback() {
      this.previewViewport?.removeEventListener("resize", this.onPreviewViewportResize);
      this.previewViewport?.removeEventListener("scroll", this.onPreviewViewportResize);
      this.previewViewport = undefined;
    }

    updatePreviewViewport() {
      if (!this.preview || !this.previewViewport) return;
      this.style.setProperty("--preview-viewport-height", `${Math.max(240, this.previewViewport.height)}px`);
      this.style.setProperty("--preview-viewport-offset-top", `${Math.max(0, this.previewViewport.offsetTop)}px`);
    }

    async loadPreviewDashboard() {
      if (!this.preview) return;
      this.previewState = {status: "loading"};
      this.draw({preserveScroll: true});
      try {
        const response = await fetch("/api/ha-demo/state", {credentials: "same-origin", cache: "no-store"});
        if (!response.ok) throw new Error("state unavailable");
        const result = await response.json();
        if (!result || !result.states || typeof result.states !== "object" || Array.isArray(result.states)) throw new Error("state invalid");
        this.previewState = {status: "ready", states: result.states};
      } catch (_) {
        this.previewState = {status: "error"};
      }
      this.draw({preserveScroll: true});
    }

    chooseQuickReply(prompt) {
      if (!this.preview || typeof prompt !== "string" || this.busy || !this.sessionId) return;
      const textarea = this.q("textarea");
      const draft = textarea.value;
      if (draft.trim()) {
        textarea.value = appendQuickReplyToDraft(draft, prompt);
        textarea.focus();
        return;
      }
      void this.send(prompt, {clearDraft: true});
    }

    updateLanguage() {
      if (!this.shadowRoot.firstChild) return;
      const t = this.t;
      const launcher = this.q(".launcher");
      launcher.title = t.open;
      launcher.setAttribute("aria-label", t.open);
      this.q("#chat-title").textContent = t.title;
      this.q(".fresh").textContent = t.fresh;
      this.q(".close").setAttribute("aria-label", t.close);
      this.q(".mic").title = t.mic;
      this.q(".mic").setAttribute("aria-label", t.mic);
      this.q("#messages").setAttribute("aria-label", t.title);
      const textarea = this.q("textarea");
      textarea.placeholder = this.preview ? t.previewPlaceholder : t.placeholder;
      textarea.setAttribute("aria-label", this.preview ? t.previewPlaceholder : t.placeholder);
      this.q(".recovery a").textContent = t.settings;
      this.q(".retry").textContent = t.retry;
      this.q(".send").title = t.send;
      this.q(".send").setAttribute("aria-label", t.send);
      this.q(".stop").textContent = t.stop;
      this.draw();
    }

    open(params = {}, opener = null, options = {}) {
      if (!this.isConnected || (!this.demo && !this.hass?.user?.is_admin) || !params || typeof params !== "object") return false;
      this.previewFocusComposer = !(options && typeof options === "object" && options.focus === false);
      this.openParams = {
        ...params,
        pipeline_id: typeof params.pipeline_id === "string" ? params.pipeline_id : "last_used",
        invalid_pipeline_id: params.pipeline_id !== undefined && (typeof params.pipeline_id !== "string" || !params.pipeline_id || params.pipeline_id.length > 256),
        start_listening: params.start_listening === true,
        initial_prompt: typeof params.prompt === "string" && params.prompt.length <= 4000 ? params.prompt : undefined,
        submit_prompt: params.submit === true && typeof params.prompt === "string" && params.prompt.length <= 4000,
      };
      if (this.openParams.initial_prompt !== undefined) this.q("textarea").value = this.openParams.initial_prompt;
      this.returnFocus = opener || this.q(".launcher");
      if (this.preview) this.setAttribute("data-preview-open", "");
      const dialog = this.q("dialog");
      if (!dialog.open) {
        if (matchMedia(this.preview ? "(max-width: 720px)" : "(max-width: 600px)").matches) dialog.showModal();
        else dialog.show();
      }
      if (this.integrationCard) this.integrationCard.active = true;
      if (this.previewFocusComposer) this.q("textarea").focus({preventScroll: true});
      else if (this.preview) this.q(".close").focus({preventScroll: true});
      if (!this.sessionId && !this.busy) void this.connect();
      else if (this.sessionId && !this.busy) this.startPendingAssist();
      return true;
    }

    onClose() {
      this.voiceGeneration += 1;
      if (this.integrationCard) this.integrationCard.active = false;
      if (this.voice) this.voice.close();
      this.voice = undefined;
      if (this.voiceBusy) {
        this.voiceBusy = false;
        this.busy = false;
        this.controls();
      }
      const target = this.returnFocus;
      this.returnFocus = null;
      if (target?.isConnected && typeof target.focus === "function") target.focus();
      else this.q(".launcher").focus();
    }

    status(text, error = false) {
      this.q("#status").textContent = text;
      this.q("#status").classList.toggle("error", error);
      this.q(".recovery").hidden = !error;
      this.q(".recovery a").hidden = this.demo;
    }

    controls() {
      this.q("textarea").disabled = !this.sessionId || this.busy || this.setupRestarting;
      this.q(".mic").disabled = this.demo || !this.sessionId || (this.busy && !this.voiceBusy) || this.setupRestarting;
      this.q(".mic").hidden = this.demo;
      this.q(".mic").title = this.voiceBusy ? this.t.stopVoice : this.t.mic;
      this.q(".mic").setAttribute("aria-label", this.voiceBusy ? this.t.stopVoice : this.t.mic);
      this.q(".send").disabled = !this.sessionId || this.busy || this.setupRestarting;
      this.q(".send").hidden = this.busy && !this.preview;
      this.q(".stop").hidden = !this.running || this.demo;
      this.q(".fresh").disabled = this.busy || !this.sessionId;
      this.q(".retry").disabled = this.busy;
      for (const button of this.shadowRoot.querySelectorAll(".quick-reply")) button.disabled = this.busy || !this.sessionId;
    }

    async connect(fresh = false) {
      if (this.busy) return;
      this.busy = true;
      this.status(this.t.loading);
      this.controls();
      try {
        const result = this.demo
          ? await this.demoRequest(fresh ? "/reset" : "", fresh ? "POST" : "GET")
          : await this.hass.callWS({type: "habab_home/agent/session", new: fresh});
        this.sessionId = result.session_id;
        this.messages = result.messages;
        this.draw();
        this.status(this.demo ? this.t.demo : "");
        await this.loadSetup();
      } catch (error) { this.status(this.t[error.code] || this.t.failed, true); }
      finally {
        this.busy = false; this.controls();
        if (this.q("dialog").open && this.previewFocusComposer) this.q("textarea").focus({preventScroll: true});
        this.startPendingAssist();
      }
    }

    async demoRequest(suffix = "", method = "GET", text) {
      const response = await fetch(`/api/ha-demo/chat${suffix}`, {
        method, credentials: "same-origin", headers: {"Content-Type": "application/json"},
        ...(text !== undefined ? {body: demoChatRequestBody(text, this.preview === true)} : {}),
      });
      const result = await response.json();
      if (!response.ok) throw {code: result.error};
      return {session_id: "public-demo", messages: result.messages.map(item => ({role: item.role, text: item.content}))};
    }

    startPendingAssist() {
      if (!this.sessionId || this.busy || this.demo || !this.q("dialog").open || !this.openParams) return;
      if (this.openParams.submit_prompt && this.openParams.initial_prompt) {
        const prompt = this.openParams.initial_prompt;
        this.openParams.submit_prompt = false;
        void this.send(prompt, {clearDraft: true});
        return;
      }
      if (this.openParams.start_listening) {
        this.openParams.start_listening = false;
        void this.startVoice(this.openParams.pipeline_id, this.openParams.invalid_pipeline_id);
      }
    }

    async errorMessage(error) {
      if (this.demo || !this.sessionId) return this.t[error?.code] || this.t.failed;
      try {
        const result = await this.hass.callWS({type: "habab_home/agent/diagnostics", session_id: this.sessionId});
        const hint = assistantDiagnosticMessage(result?.error, this.t);
        return hint ? `${this.t.failed}${hint}` : (this.t[error?.code] || this.t.failed);
      } catch (_) {
        return this.t[error?.code] || this.t.failed;
      }
    }

    async pipelines() {
      if (this.assistPipelines) return this.assistPipelines;
      const result = await this.hass.callWS({type: "assist_pipeline/pipeline/list"});
      if (!Array.isArray(result?.pipelines)) throw {code: "invalid_pipeline_list"};
      this.assistPipelines = result.pipelines;
      this.preferredPipelineId = result.preferred_pipeline;
      return this.assistPipelines;
    }

    async startVoice(requestedId = "last_used", invalidId = false) {
      if (this.demo || this.busy || !this.sessionId) return;
      const generation = ++this.voiceGeneration;
      this.voiceBusy = true;
      this.busy = true;
      this.pendingVoiceText = undefined;
      this.controls();
      try {
        if (invalidId) throw {code: "noAssistPipeline"};
        const pipelines = await this.pipelines();
        if (generation !== this.voiceGeneration || !this.q("dialog").open) return;
        let lastUsedId;
        try { lastUsedId = JSON.parse(localStorage.getItem("AssistPipelineId")); } catch (_) { lastUsedId = undefined; }
        const pipeline = chooseAssistPipeline(requestedId, pipelines, this.preferredPipelineId, lastUsedId);
        if (!pipeline) throw {code: "noAssistPipeline"};
        if (!pipeline.stt_engine) throw {code: "speechRequired"};
        this.voiceReplyPipeline = pipeline.tts_engine ? pipeline : undefined;
        this.status(this.t.listening);
        this.voice = new AssistVoice(this.hass, pipeline, (event) => this.onVoiceEvent(event));
        await this.voice.start();
      } catch (error) {
        if (generation !== this.voiceGeneration) return;
        this.voice?.close();
        this.voice = undefined;
        this.voiceBusy = false;
        this.busy = false;
        const code = typeof error?.code === "string" && this.t[error.code] ? error.code : "failed";
        this.status(this.t[code], true);
        this.controls();
      }
    }

    onVoiceEvent(event) {
      if (event.type === "stt-vad-start") this.status(this.t.speechStart);
      if (event.type === "stt-vad-end" || event.type === "recording-ended") this.status(this.t.speechEnd);
      if (event.type === "stt-end") {
        const text = event.data?.stt_output?.text;
        this.pendingVoiceText = typeof text === "string" ? text.trim() : "";
      }
      if (event.type === "playback-blocked") this.status(this.t.voiceReplyFailed, true);
      if (event.type === "error") {
        this.pendingVoiceText = undefined;
        this.status(this.t[this.voiceErrorCode(event.data?.code)] || this.t.failed, true);
      }
      if (event.type === "run-end" || event.type === "error") {
        const transcript = this.pendingVoiceText;
        this.pendingVoiceText = undefined;
        this.voice?.close(false);
        this.voice = undefined;
        this.voiceBusy = false;
        this.busy = false;
        this.controls();
        if (event.type === "run-end") {
          if (transcript) void this.send(transcript, {voiceReply: true});
          else this.status(this.t.transcriptEmpty, true);
        }
      }
    }

    voiceErrorCode(code) {
      if (code === "microphone_denied" || code === "microphone_failed" || code === "secure_context" || code === "timeout") return code;
      if (code === "not_found") return "noAssistPipeline";
      return "failed";
    }

    async speakReply(messages) {
      if (!this.q("dialog").open) return;
      const message = Array.isArray(messages) ? [...messages].reverse().find((item) => item?.role === "assistant" && typeof item.text === "string" && item.text.trim()) : undefined;
      const pipeline = this.voiceReplyPipeline;
      this.voiceReplyPipeline = undefined;
      if (!message || !pipeline?.tts_engine) return;
      const voice = new AssistVoice(this.hass, pipeline, (event) => {
        if (event.type === "playback-blocked" || event.type === "error") {
          this.status(this.t.voiceReplyFailed, true);
          voice.close();
          if (this.voice === voice) this.voice = undefined;
        }
        if (event.type === "playback-ended" && this.voice === voice) this.voice = undefined;
      });
      this.voice = voice;
      try { await voice.speak(message.text); }
      catch (_) {
        this.status(this.t.voiceReplyFailed, true);
        voice.close();
        if (this.voice === voice) this.voice = undefined;
      }
      finally {
        if (!voice.audio && this.voice === voice) {
          voice.close(false);
          this.voice = undefined;
        }
      }
    }

    draw({preserveScroll = false} = {}) {
      const log = this.q("#log");
      const previousScrollTop = log.scrollTop;
      const messageContainer = this.q("#messages");
      messageContainer.replaceChildren();
      const visibleMessages = visibleTextMessages(this.messages);
      if (this.preview) messageContainer.append(this.renderPreviewMessage());
      if (!visibleMessages.length && !this.preview) {
        const empty = document.createElement("div"); empty.className = "empty";
        const title = document.createElement("h3"); title.textContent = this.t.intro;
        const help = document.createElement("p"); help.textContent = this.t.help;
        empty.append(title, help); messageContainer.append(empty);
      }
      for (const message of visibleMessages) {
        const row = document.createElement("div"); row.className = `message ${message.role}`;
        const name = document.createElement("div"); name.className = "name"; name.textContent = message.role === "user" ? this.t.you : this.t.assistant;
        const markdown = message.role === "assistant" && customElements.get("ha-markdown");
        const text = document.createElement(markdown ? "ha-markdown" : "p");
        if (markdown) {text.content = message.text; text.breaks = true;}
        else text.textContent = message.text;
        row.append(name, text); messageContainer.append(row);
      }
      const keepPreviewIntroVisible = this.preview && this.sessionId && !this.previewHistoryPositioned;
      log.scrollTop = preserveScroll ? previousScrollTop : keepPreviewIntroVisible ? 0 : log.scrollHeight;
      if (keepPreviewIntroVisible) this.previewHistoryPositioned = true;
      const tail = log.lastChild;
      Promise.allSettled([...messageContainer.querySelectorAll("ha-markdown")].map(item => item.updateComplete)).then(() => {
        if (log.lastChild === tail && !keepPreviewIntroVisible && !preserveScroll) log.scrollTop = log.scrollHeight;
      });
    }

    renderPreviewMessage() {
      const section = document.createElement("section");
      section.className = "preview-message";
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = this.t.assistant;
      const question = document.createElement("h3");
      question.textContent = this.t.previewQuestion;
      section.append(name, question);

      const dashboard = document.createElement("div");
      dashboard.className = "preview-dashboard";
      const heading = document.createElement("h4");
      heading.textContent = this.t.previewDashboard;
      dashboard.append(heading);
      if (this.previewState.status === "loading" || this.previewState.status === "idle") {
        const message = document.createElement("p");
        message.className = "preview-state";
        message.textContent = this.t.dashboardLoading;
        dashboard.append(message);
      } else if (this.previewState.status === "error") {
        const message = document.createElement("p");
        message.className = "preview-state error";
        message.textContent = this.t.dashboardFailed;
        const retry = document.createElement("button");
        retry.className = "preview-retry";
        retry.type = "button";
        retry.textContent = this.t.dashboardRetry;
        dashboard.append(message, retry);
      } else {
        const rows = buildPreviewRows(this.previewState.states, this.language);
        if (!rows.length) {
          const message = document.createElement("p");
          message.className = "preview-state";
          message.textContent = this.t.dashboardEmpty;
          dashboard.append(message);
        } else {
          const rowList = document.createElement("div");
          rowList.className = "preview-rows";
          for (const item of rows) {
            const row = document.createElement("div");
            row.className = "preview-row";
            const label = document.createElement("span");
            label.className = "preview-label";
            label.textContent = item.label;
            const value = document.createElement("span");
            value.className = "preview-value";
            value.textContent = item.state === "on" ? this.t.stateOn
              : item.state === "off" ? this.t.stateOff
                : item.state === "heat" ? this.t.stateHeat : this.t.stateUnavailable;
            row.append(label, value);
            rowList.append(row);
          }
          dashboard.append(rowList);
        }
      }
      section.append(dashboard);

      const quickReplies = document.createElement("div");
      quickReplies.className = "quick-replies";
      const choices = [
        [this.t.quickXiaomi, this.language === "ru" ? "У меня есть устройства Xiaomi Home" : "I have Xiaomi Home devices"],
        [this.t.quickZigbee, this.language === "ru" ? "Хочу подключить устройства Zigbee" : "I want to connect Zigbee devices"],
        [this.t.quickOther, this.language === "ru" ? "У меня есть другое оборудование" : "I have other equipment"],
      ];
      for (const [label, prompt] of choices) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "quick-reply";
        button.textContent = label;
        button.dataset.prompt = prompt;
        button.disabled = this.busy || !this.sessionId;
        quickReplies.append(button);
      }
      section.append(quickReplies);
      return section;
    }

    async send(textValue, {voiceReply = false, clearDraft = false} = {}) {
      const text = (typeof textValue === "string" ? textValue : this.q("textarea").value).trim();
      if (!text || !this.sessionId || this.busy || this.setupRestarting) return;
      this.busy = true; this.running = true; this.stopped = false;
      this.messages.push({role: "user", text});
      if (typeof textValue !== "string" || clearDraft) this.q("textarea").value = "";
      this.draw(); this.status(this.demo ? this.t.answering : this.t.working); this.controls();
      try {
        const result = this.demo ? await this.demoRequest("", "POST", text)
          : await this.hass.callWS({type: "habab_home/agent/run", session_id: this.sessionId, text});
        this.messages = result.messages; this.draw(); this.status(this.stopped ? this.t.stopped : this.demo ? this.t.demo : "");
        if (voiceReply) await this.speakReply(result.messages);
      } catch (error) { this.status(this.stopped ? this.t.stopped : await this.errorMessage(error), true); }
      finally {
        if (!this.demo) await this.loadSetup(true);
        this.busy = false; this.running = false; this.controls();
        if (this.q("dialog").open && this.previewFocusComposer) this.q("textarea").focus({preventScroll: true});
        this.startPendingAssist();
      }
    }

    async stop() {
      this.q(".stop").disabled = true;
      try {
        const result = await this.hass.callWS({type: "habab_home/agent/abort", session_id: this.sessionId});
        this.stopped = result.stopped;
        if (this.stopped) this.status(this.t.stopped);
      } catch (error) { this.status(this.t[error.code] || this.t.failed, true); }
      finally { this.q(".stop").disabled = false; }
    }
  }
  customElements.define("habab-setup-chat", HababSetupChat);

  function locateMain(root) {
    for (const element of root.querySelectorAll("*")) {
      if (element.localName === "home-assistant-main") return element;
      if (element.shadowRoot) {const found = locateMain(element.shadowRoot); if (found) return found;}
    }
  }

  // A downstream module must also work with the stock Docker frontend. Keep the
  // search off the render loop and reuse the element across dashboard updates.
  let launcher;
  const assistantHook = {
    get available() {
      return !!launcher && launcher.parentNode === document.body && !launcher.demo && launcher.hass?.user?.is_admin === true;
    },
    open(params, opener) {
      if (!this.available || !launcher) return false;
      return launcher.open(params, opener);
    },
  };
  let lastAssistantAvailability = false;
  const syncAssistantHook = () => {
    const available = assistantHook.available;
    if (available) window.hababAssistant = assistantHook;
    else if (window.hababAssistant === assistantHook) delete window.hababAssistant;
    if (available !== lastAssistantAvailability) {
      lastAssistantAvailability = available;
      window.dispatchEvent(new Event("habab-assistant-availability"));
    }
  };
  const syncLauncher = () => {
    const main = locateMain(document);
    const demo = document.documentElement.dataset.hababDemo === "true";
    const preview = isOnboardingPreview(demo, document.documentElement.dataset.hababDemoPreview);
    const hass = main?.hass;
    if (!demo && !hass?.user?.is_admin) {launcher?.remove(); launcher = undefined; syncAssistantHook(); return;}
    if (!demo && !main) {launcher?.remove(); launcher = undefined; syncAssistantHook(); return;}
    if (!launcher) {
      launcher = document.createElement("habab-setup-chat");
      launcher.demo = demo;
      launcher.preview = preview;
      if (preview) launcher.setAttribute("data-preview", "onboarding");
      launcher.hass = hass;
      document.body.append(launcher);
    }
    launcher.demo = demo;
    launcher.preview = preview;
    if (preview) launcher.setAttribute("data-preview", "onboarding");
    else launcher.removeAttribute("data-preview");
    launcher.hass = hass;
    syncThemeVariables(launcher, main);
    if (launcher.parentNode !== document.body) document.body.append(launcher);
    syncAssistantHook();
  };
  const themeVariables = new WeakMap();
  const themeVariableNames = [
    "--primary-text-color", "--secondary-text-color", "--primary-background-color",
    "--card-background-color", "--secondary-background-color", "--divider-color",
    "--primary-color", "--dark-primary-color", "--text-primary-color", "--error-color",
    "--ha-color-focus", "--ha-color-fill-primary-loud-hover", "--ha-color-on-primary-loud",
    "--paper-font-body1_-_font-family",
  ];
  function syncThemeVariables(element, source) {
    if (!source || typeof getComputedStyle !== "function") return;
    let synced = themeVariables.get(element);
    if (!synced) {synced = new Map(); themeVariables.set(element, synced);}
    const computed = getComputedStyle(source);
    const present = new Set();
    for (const name of themeVariableNames) {
      const value = computed.getPropertyValue(name).trim();
      if (!value) continue;
      present.add(name);
      if (synced.get(name) !== value) {
        element.style.setProperty(name, value);
        synced.set(name, value);
      }
    }
    for (const name of synced.keys()) {
      if (!present.has(name)) {
        element.style.removeProperty(name);
        synced.delete(name);
      }
    }
  }
  const observer = new MutationObserver(syncLauncher);
  observer.observe(document.documentElement, {childList: true, subtree: true});
  setInterval(syncLauncher, 1000);
  syncLauncher();
}

const registryReady = setInterval(() => {
  if (customElements.get("home-assistant")) {
    clearInterval(registryReady);
    installChat();
  }
}, 50);
