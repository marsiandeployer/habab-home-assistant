// Independent downstream extension: no dependency on compiled HA internals.
import { AssistVoice, BrowserGeminiVoice, bootstrapAfterStartCue, playVoiceCue } from "./voice.js";
import { strings } from "./strings.js?v=6";
import { chatDismissalStorageKey, decorateChatDetails, readChatDismissals } from "./chat-dismissal.js";
import { createToolCalls } from "./chat-tools.js?v=1";
export function visibleTextMessages(messages) {
  if (!Array.isArray(messages)) return [];
  return messages.filter((message) =>
    (typeof message?.text === "string" && message.text.trim().length > 0) ||
    (message?.role === "assistant" && Array.isArray(message.tool_calls) && message.tool_calls.length > 0)
  );
}

export function assistantFeedbackContext(messages, answerIndex) {
  if (!Array.isArray(messages) || !Number.isInteger(answerIndex) || answerIndex < 0 || answerIndex >= messages.length) return null;
  const answer = messages[answerIndex];
  if (answer?.role !== "assistant" || typeof answer.text !== "string" || !answer.text.trim()) return null;
  let promptText = "";
  for (let index = answerIndex - 1; index >= 0; index--) {
    if (messages[index]?.role === "user" && typeof messages[index].text === "string" && messages[index].text.trim()) {
      promptText = messages[index].text;
      break;
    }
  }
  return {answer_index: answerIndex, answer_text: answer.text, prompt_text: promptText.slice(0, 4000)};
}

function createFeedbackIdempotencyKey() {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") globalThis.crypto.getRandomValues(bytes);
  else bytes.forEach((_byte, index) => { bytes[index] = Math.floor(Math.random() * 256); });
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const MAX_ANSWER_FEEDBACK_COMMENT_CHARS = 1800;

export async function requestAssistantSession(hass, {fresh = false, isCurrent, onActivating}) {
  const request = {type: "habab_home/agent/session", new: fresh};
  try {
    const result = await hass.callWS(request);
    if (!isCurrent()) throw {code: "not_found"};
    return result;
  } catch (error) {
    if (error?.code !== "provider_required" || !isCurrent()) throw error;
    onActivating();
    const activation = await hass.callWS({type: "habab_home/beta/activate"});
    if (!isCurrent()) throw {code: "not_found"};
    if (!["ready", "existing"].includes(activation?.status)) {
      throw {code: "habab_beta_invalid_response"};
    }
    const result = await hass.callWS(request);
    if (!isCurrent()) throw {code: "not_found"};
    return result;
  }
}

export function appendNativeAssistTurn(messages, userText, assistantText) {
  const updated = Array.isArray(messages) ? [...messages] : [];
  if (typeof userText === "string" && userText.trim()) updated.push({role: "user", text: userText.trim()});
  if (typeof assistantText === "string" && assistantText.trim()) updated.push({role: "assistant", text: assistantText.trim().slice(0, 12000)});
  return updated;
}

export function classifyAssistantContext(previousOwnerId, main) {
  const hass = main?.hass;
  const user = hass?.user;
  if (!main || !hass || !user || typeof user.is_admin !== "boolean") return {state: "unknown"};
  if (!user.is_admin) return {state: "non-admin"};
  if (typeof user.id !== "string" || !user.id.trim()) return {state: "unknown"};
  if (previousOwnerId !== undefined && previousOwnerId !== user.id) {
    return {state: "different-user", ownerId: user.id};
  }
  return {state: "admin", ownerId: user.id};
}

export function appendQuickReplyToDraft(draft, prompt) {
  return typeof draft === "string" && draft.trim()
    ? `${draft.replace(/\s+$/, "")}\n${prompt}`
    : prompt;
}

export function shouldBlockFrontendReload({busy, voiceBusy, activeVoice, pendingVoiceText, draft}) {
  return busy === true || voiceBusy === true || activeVoice === true ||
    (typeof pendingVoiceText === "string" && pendingVoiceText.trim().length > 0) ||
    (typeof draft === "string" && draft.trim().length > 0);
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

function isEditableFocusTarget(target) {
  if (target instanceof HTMLInputElement) {
    return !target.matches(":disabled") && !target.readOnly &&
      target.getAttribute("aria-disabled") !== "true" && target.getAttribute("aria-readonly") !== "true" &&
      !["button", "submit", "reset", "image", "hidden"].includes(target.type);
  }
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) {
    return !target.matches(":disabled") && !target.readOnly &&
      target.getAttribute("aria-disabled") !== "true" && target.getAttribute("aria-readonly") !== "true";
  }
  return target instanceof HTMLElement && target.isContentEditable &&
    target.getAttribute("aria-disabled") !== "true" &&
    target.getAttribute("aria-readonly") !== "true";
}

export function chooseAssistPipeline(requestedId, pipelines, preferredId, lastUsedId) {
  if (!Array.isArray(pipelines)) return undefined;
  let id = requestedId === "preferred" ? preferredId
    : requestedId === "last_used" || !requestedId ? lastUsedId || preferredId
      : requestedId;
  return pipelines.find((pipeline) => pipeline?.id === id);
}

export function selectBrowserVoiceRoute(capability) {
  if (capability?.available === true && capability.client === "browser_v1" &&
      capability.provider === "habab" && capability.realtime_provider === "gemini" &&
      capability.transport === "habab" && capability.websocket_url === "wss://habab-voice.wpmix.net/gemini/ws" &&
      capability.auth?.mode === "first_text_frame" && capability.auth?.type === "habab.voice.auth.v1") {
    return "habab_browser_v1";
  }
  if (capability?.available === false &&
      capability.reason === "selected_provider_requires_ha_assist_stt" &&
      capability.browser?.transport === "ha_assist_pipeline" &&
      capability.browser?.requires_ha_stt === true) {
    return "ha_assist_pipeline";
  }
  return undefined;
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

export function getXiaomiSetupPresentation(phase, dismissed) {
  const dismissible = phase === "error";
  return {
    showCard: phase !== "completed" && !(dismissible && dismissed === true),
    clearDismissal: phase !== "completed" && !dismissible && dismissed === true,
  };
}

function installChat() {
  // HA installs its scoped registry polyfill before defining home-assistant.
  // Registering earlier loses the extension when that registry is replaced.
  if (customElements.get("habab-setup-chat")) return;
  const copy = {
    ru: {
      title: "Помощник", open: "Открыть чат с помощником", close: "Закрыть", fresh: "Новый чат",
      intro: "Что подключим к дому? Какое оборудование у вас уже есть?",
      quickXiaomi: "Xiaomi Home", quickZigbee: "Zigbee", quickOther: "Другое",
      collapseChat: "Показать панель дома",
      placeholder: "Например: хочу подключить пылесос Xiaomi", send: "Отправить", stop: "Остановить",
      working: "Проверяю и выполняю…", loading: "Подключаю помощника…", you: "Вы", assistant: "Помощник",
      answering: "Готовлю ответ…", rate_limited: "Слишком много запросов. Повторите позже.",
      settings: "Подключить AI", retry: "Повторить подключение",
      setup_ui_unavailable: "Не удалось открыть настройку устройств. Обновите страницу и попробуйте снова.",
      deepseek_required: "Сначала подключите DeepSeek в разделе AI.",
      provider_required: "Сначала подключите провайдера ИИ в разделе AI.",
      activatingBeta: "Подключаем бесплатную бету Habab…",
      habab_beta_issuance_disabled: "Выдача новых кодов беты завершена. Свой AI-провайдер можно подключить в разделе AI.",
      habab_beta_unavailable: "Не удалось подключить бету Habab. Повторите подключение.",
      habab_beta_cannot_connect: "Нет соединения с Habab. Проверьте интернет и повторите подключение.",
      habab_beta_timeout: "Habab не ответил вовремя. Повторите подключение.",
      habab_beta_invalid_response: "Не удалось подтвердить подключение к Habab. Повторите попытку.",
      habab_beta_setup_failed: "Не удалось сохранить подключение Habab. Повторите попытку.",
      habab_beta_storage_failed: "Не удалось сохранить код установки. Повторите попытку.",
      habab_beta_rate_limited: "Слишком много попыток подключения к Habab. Повторите немного позже.",
      provider_not_ready: "Текстовый сервис Habab не готов к запросам. Повторите подключение позже.",
      not_installed: "OpenCode ещё не подключён к контейнеру. Добавьте compose.opencode.yaml по инструкции установки.",
      binary_changed: "Файл OpenCode изменился. Повторите prepare-opencode.py и пересоздайте контейнер.",
      busy: "Помощник уже выполняет задачу. Дождитесь завершения и повторите подключение.",
      stopped: "Выполнение остановлено. Уже сделанные изменения могли сохраниться.",
      failed: "Не удалось завершить запрос. Некоторые действия могли выполниться. Повторите подключение, чтобы проверить историю.",
      runtime_timeout: "Запрос превысил время ожидания. Действия могли выполниться. Проверьте состояние дома и историю перед повтором.",
      showSetupForm: "Открыть форму настройки",
      openingDevices: "Открываю главную с пылесосом…", devicesReady: "Пылесос в избранном на главной.",
      noVacuumDevices: "Пылесос пока не найден. После подключения снова нажмите «Показать пылесос на главной».",
      devicesFailed: "Не удалось показать пылесос на главной. Проверьте права администратора и повторите.",
      diagnosticInvalidModel: " Проверьте имя модели в настройках провайдера.",
      diagnosticQuota: " Проверьте баланс и лимиты запросов у провайдера.",
      diagnosticAuth: " Проверьте API-ключ и доступ к модели в настройках провайдера.",
      diagnosticContext: " Сократите запрос или начните новый чат.",
      diagnosticTimeout: " Запрос превысил время ожидания; действия могли выполниться. Проверьте состояние дома перед повтором команды.",
      diagnosticReasoning: " Проверьте, что выбранная модель совместима с OpenCode.",
      diagnosticTools: " Выберите модель с поддержкой инструментов.",
      diagnosticGeneric: " Проверьте подключение и настройки провайдера.",
      noAssistPipeline: "Голосовой помощник недоступен. Настройте его в Home Assistant и повторите попытку.",
      speechRequired: "Выбранный голосовой помощник не настроен для распознавания речи. Выберите другой в настройках голосовых помощников.",
      transcriptEmpty: "Не удалось распознать речь. Попробуйте ещё раз или введите запрос.",
      voiceReplyFailed: "Не удалось воспроизвести голосовой ответ. Ответ остался в чате.",
      mic: "Сказать запрос помощнику", stopVoice: "Завершить запись", listening: "Слушаю. Завершу запись после паузы в речи.",
      voiceModeLabel: "Режим голоса",
      voiceModeHabab: "Habab",
      voiceModeAssist: "Assist",
      hideChatBlock: "Скрыть блок",
      nativeAssistTitle: "Обработка идёт через выбранный pipeline Home Assistant Assist; он может использовать облачные сервисы. Если TTS не настроен, ответ появится только текстом.",
      assistPipelineLabel: "Голосовой помощник Home Assistant",
      assistPipelineDefault: "Использовать выбранный в Home Assistant",
      assistPipelineUnavailable: "Выбранный голосовой помощник больше недоступен. Выберите другой в списке.",
      assistPipelinesFailed: "Не удалось загрузить голосовые помощники Home Assistant.",
      nativeAssistMic: "Сказать через выбранный pipeline Home Assistant Assist",
      nativeAssistHistory: "Ответ обработан выбранным pipeline Assist Home Assistant. Эти реплики видны только в текущем экране; Habab их не сохраняет.",
      nativeAssistFailed: "Выбранный pipeline Assist Home Assistant не вернул текст ответа.",
      nativeAssistWorking: "Обрабатываю через выбранный pipeline Assist Home Assistant…",
      speechStart: "Слышу речь…", speechEnd: "Пауза определена. Распознаю речь…", speechNotFound: "Голосовой помощник недоступен.",
      secure_context: "Микрофону нужен HTTPS или localhost. Откройте Home Assistant через защищённое соединение.",
      microphone_denied: "Доступ к микрофону запрещён. Разрешите его в настройках браузера.",
      microphone_failed: "Не удалось включить микрофон. Проверьте устройство и повторите.",
      timeout: "Время ожидания истекло. Проверьте состояние дома перед повтором команды.",
    },
    en: {
      title: "Assistant", open: "Open assistant chat", close: "Close", fresh: "New chat",
      intro: "What should we connect to your home? What equipment do you already have?",
      quickXiaomi: "Xiaomi Home", quickZigbee: "Zigbee", quickOther: "Other",
      collapseChat: "Show home dashboard",
      placeholder: "For example: I want to connect a Xiaomi vacuum", send: "Send", stop: "Stop",
      working: "Checking and working…", loading: "Connecting…", you: "You", assistant: "Assistant",
      answering: "Preparing an answer…", rate_limited: "Too many requests. Try again later.",
      settings: "Connect AI", retry: "Reconnect",
      setup_ui_unavailable: "Device setup could not open. Refresh the page and try again.",
      deepseek_required: "Connect DeepSeek in AI settings first.",
      provider_required: "Connect an AI provider in AI settings first.",
      activatingBeta: "Connecting to the free Habab beta…",
      habab_beta_issuance_disabled: "New beta registrations have closed. You can connect your own AI provider in AI settings.",
      habab_beta_unavailable: "Could not connect to the Habab beta. Try reconnecting.",
      habab_beta_cannot_connect: "Could not reach Habab. Check your internet connection and try again.",
      habab_beta_timeout: "Habab did not respond in time. Try reconnecting.",
      habab_beta_invalid_response: "Could not verify the Habab connection. Try again.",
      habab_beta_setup_failed: "Could not save the Habab connection. Try again.",
      habab_beta_storage_failed: "Could not save the installation code. Try again.",
      habab_beta_rate_limited: "Too many connection attempts. Try again shortly.",
      provider_not_ready: "Habab is not ready for text requests. Try connecting again later.",
      not_installed: "OpenCode is not mounted. Follow the installation guide to add compose.opencode.yaml.",
      binary_changed: "The OpenCode executable changed. Run prepare-opencode.py again and recreate the container.",
      busy: "The assistant is already working. Wait for it to finish, then reconnect.",
      stopped: "Stopped. Changes already made may remain.",
      failed: "The request did not finish. Some actions may have run. Reconnect to check the history.",
      runtime_timeout: "The request timed out. Actions may have run. Check the home state and history before repeating it.",
      showSetupForm: "Open setup form",
      openingDevices: "Opening Home with your vacuum…", devicesReady: "The vacuum is in your Home favorites.",
      noVacuumDevices: "No vacuum was found. After connecting it, choose “Show vacuum on Home” again.",
      devicesFailed: "Could not show the vacuum on Home. Check administrator access and retry.",
      diagnosticInvalidModel: " Check the model name in provider settings.",
      diagnosticQuota: " Check your provider balance and request limits.",
      diagnosticAuth: " Check the API key and model access in provider settings.",
      diagnosticContext: " Shorten the request or start a new chat.",
      diagnosticTimeout: " The request timed out; actions may have run. Check the home state before repeating the command.",
      diagnosticReasoning: " Check that the selected model works with OpenCode.",
      diagnosticTools: " Choose a model that supports tools.",
      diagnosticGeneric: " Check your connection and provider settings.",
      noAssistPipeline: "A voice assistant is unavailable. Configure one in Home Assistant and try again.",
      speechRequired: "The selected voice assistant is not configured for speech recognition. Choose another in Voice assistant settings.",
      transcriptEmpty: "Speech could not be recognized. Try again or enter a request.",
      voiceReplyFailed: "The spoken reply could not play. The response is still in chat.",
      mic: "Send a voice request to the assistant", stopVoice: "Finish recording", listening: "Listening. Recording ends after a pause in speech.",
      voiceModeLabel: "Voice mode",
      voiceModeHabab: "Habab",
      voiceModeAssist: "Assist",
      hideChatBlock: "Hide block",
      nativeAssistTitle: "Uses the selected Home Assistant Assist pipeline, which may use cloud services. If TTS is not configured, the response appears as text only.",
      assistPipelineLabel: "Home Assistant voice assistant",
      assistPipelineDefault: "Use the Home Assistant selection",
      assistPipelineUnavailable: "The selected voice assistant is no longer available. Choose another from the list.",
      assistPipelinesFailed: "Home Assistant voice assistants could not be loaded.",
      nativeAssistMic: "Send voice through the selected Home Assistant Assist pipeline",
      nativeAssistHistory: "Handled by the selected Home Assistant Assist pipeline. These messages appear only on this screen; Habab does not save them.",
      nativeAssistFailed: "The selected Home Assistant Assist pipeline returned no response text.",
      nativeAssistWorking: "Processing through the selected Home Assistant Assist pipeline…",
      speechStart: "Speech detected…", speechEnd: "Pause detected. Transcribing…", speechNotFound: "A voice assistant is unavailable.",
      secure_context: "Microphone access requires HTTPS or localhost. Open Home Assistant using a secure connection.",
      microphone_denied: "Microphone access was denied. Allow access in your browser settings.",
      microphone_failed: "The microphone could not start. Check your device and try again.",
      timeout: "The request timed out. Check the home state before repeating the command.",
    },
  };
  // Keep the chat's existing local copy, while making newer shared UI strings
  // (including answer feedback states) available through this component's getter.
  copy.ru = {...strings.ru, ...copy.ru};
  copy.en = {...strings.en, ...copy.en};

  function installAssistantSplitStyles() {
    if (!document.head || document.getElementById?.("habab-assistant-split-style")) return;
    const style = document.createElement("style");
    style.id = "habab-assistant-split-style";
    style.textContent = `
      :root {
        --habab-assistant-open-inline-size: 0px;
        --habab-assistant-open-block-size: 0px;
      }
      @media (min-width: 721px) {
        html[data-habab-assistant-open="true"] {
          --habab-assistant-chat-width: max(420px, 50vw);
          --habab-assistant-open-inline-size: var(--habab-assistant-chat-width);
        }
        html[data-habab-assistant-open="true"] body > ha-demo,
        html[data-habab-assistant-open="true"] body > home-assistant {
          display: block !important;
          width: calc(100% - var(--habab-assistant-chat-width)) !important;
          max-width: calc(100% - var(--habab-assistant-chat-width)) !important;
          min-width: 0 !important;
          margin-inline: 0 !important;
          height: 100dvh !important;
          overflow: auto !important;
        }
      }
      @media (max-width: 720px) {
        html[data-habab-assistant-open="true"] {
          --habab-assistant-open-block-size: 50dvh;
        }
        html[data-habab-assistant-open="true"][data-habab-assistant-expanded="true"] {
          --habab-assistant-open-block-size: var(--assistant-viewport-height, 100dvh);
        }
        html[data-habab-assistant-open="true"] body > ha-demo,
        html[data-habab-assistant-open="true"] body > home-assistant {
          display: block !important;
          width: 100% !important;
          max-width: 100% !important;
          min-width: 0 !important;
          height: 50dvh !important;
          overflow: auto !important;
        }
        html[data-habab-assistant-open="true"][data-habab-assistant-expanded="true"] body > ha-demo,
        html[data-habab-assistant-open="true"][data-habab-assistant-expanded="true"] body > home-assistant {
          height: 0 !important;
          overflow: hidden !important;
          visibility: hidden !important;
          pointer-events: none !important;
        }
      }
    `;
    document.head.append(style);
  }

  class HababSetupChat extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({mode: "open"});
      this.messages = [];
      this.feedbackStates = new Map();
      this.busy = false;
      this.voiceBusy = false;
      this.voiceGeneration = 0;
      this.voiceMode = "habab";
      this.assistPipelineId = undefined;
      this.assistPipelineChoiceSet = false;
      this.nativeAssistRun = false;
      this.nativeAssistPipeline = undefined;
      this.nativeAssistConversation = undefined;
      this.pendingNativeAssistResponse = undefined;
      this.nativeAssistBusyGeneration = undefined;
      this.contextAvailable = false;
      this.ownerId = undefined;
      this.autoOpenStarted = false;
      this.mobileExpanded = false;
      this.layoutLifecycleInstalled = false;
      this.markdownUpgradePending = false;
      this.markdownUpgradeLifecycle = 0;
      this.drawGeneration = 0;
      this.followSetupForms = true;
      this.suppressedSetupInteractionIds = new Set();
      this.freshChatSetupInteractionIds = new Set();
      this.freshSetupSnapshotPending = false;
      this.freshSetupSnapshotArmed = false;
      this.freshXiaomiSnapshotPending = false;
      this.suppressedXiaomiSetupId = undefined;
      this.freshChatXiaomiSetupId = undefined;
      this.setupLoadGeneration = 0;
      this.xiaomiSetupDismissedInstance = false;
      this.xiaomiSetupDismissalIdentity = undefined;
      this.xiaomiSetupDismissalStorageFailed = false;
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
      if (this.automationCard) this.automationCard.hass = value;
      this.subscribeVoiceHistory();
    }

    isCurrentOwnerContext() {
      if (this.demo) return true;
      if (!this.isConnected) return false;
      if (!this.contextAvailable || this.hidden || this.inert || typeof this.ownerId !== "string") return false;
      const current = classifyAssistantContext(this.ownerId, locateMain(document));
      return current.state === "admin" && current.ownerId === this.ownerId &&
        this.hass?.user?.is_admin === true && this.hass.user.id === this.ownerId;
    }

    xiaomiSetupDismissalKey() {
      const user = this.hass?.user;
      if (user?.is_admin !== true || typeof user.id !== "string" || !user.id || typeof window === "undefined") return null;
      const origin = window.location?.origin;
      if (typeof origin !== "string" || !origin) return null;
      return `habab-home-xiaomi-setup-dismissed:${encodeURIComponent(origin)}:${encodeURIComponent(user.id)}`;
    }

    getXiaomiSetupDismissed() {
      const key = this.xiaomiSetupDismissalKey();
      const identity = key || null;
      if (identity !== this.xiaomiSetupDismissalIdentity) {
        this.xiaomiSetupDismissalIdentity = identity;
        this.xiaomiSetupDismissedInstance = false;
        this.xiaomiSetupDismissalStorageFailed = false;
      }
      if (!key || this.xiaomiSetupDismissalStorageFailed) return this.xiaomiSetupDismissedInstance;
      try {
        return window.localStorage.getItem(key) === "1";
      } catch (_) {
        this.xiaomiSetupDismissalStorageFailed = true;
        return this.xiaomiSetupDismissedInstance;
      }
    }

    setXiaomiSetupDismissed(dismissed) {
      const key = this.xiaomiSetupDismissalKey();
      const identity = key || null;
      if (identity !== this.xiaomiSetupDismissalIdentity) {
        this.xiaomiSetupDismissalIdentity = identity;
        this.xiaomiSetupDismissedInstance = false;
        this.xiaomiSetupDismissalStorageFailed = false;
      }
      this.xiaomiSetupDismissedInstance = dismissed === true;
      if (!key) return;
      try {
        if (this.xiaomiSetupDismissedInstance) window.localStorage.setItem(key, "1");
        else window.localStorage.removeItem(key);
        this.xiaomiSetupDismissalStorageFailed = false;
      } catch (_) {
        this.xiaomiSetupDismissalStorageFailed = true;
      }
    }

    updateXiaomiSetupPresentation(phase) {
      const setupId = this.setupCard?.state?.setup_id;
      if (this.freshXiaomiSnapshotPending && typeof setupId === "string" && setupId.length > 0) {
        this.freshChatXiaomiSetupId = setupId;
        if (this.setupCard) this.setupCard.hidden = true;
        this.persistSetupBaseline();
      }
      const presentation = getXiaomiSetupPresentation(phase, this.getXiaomiSetupDismissed());
      if (presentation.clearDismissal) this.setXiaomiSetupDismissed(false);
      const container = this.q("#setup-cards");
      if (presentation.showCard) {
        if (this.integrationCard?.parentNode === container) {
          if (this.setupCard.parentNode !== container || this.setupCard.nextSibling !== this.integrationCard) {
            container.insertBefore(this.setupCard, this.integrationCard);
          }
        } else {
          mountStableElement(container, this.setupCard);
        }
      } else this.setupCard.remove();
      this.setupCard.hidden = this.isXiaomiSetupSuppressed();
    }

    isXiaomiSetupSuppressed() {
      const setupId = this.setupCard?.state?.setup_id;
      return typeof setupId === "string" && setupId.length > 0 &&
        (setupId === this.suppressedXiaomiSetupId || setupId === this.freshChatXiaomiSetupId);
    }

    allSuppressedSetupInteractionIds() {
      return new Set([...(this.suppressedSetupInteractionIds || []), ...(this.freshChatSetupInteractionIds || [])]);
    }

    setupScopeStorageKey() {
      const user = this.hass?.user;
      const origin = typeof window === "undefined" ? undefined : window.location?.origin;
      if (user?.is_admin !== true || typeof user.id !== "string" || !user.id ||
          typeof origin !== "string" || !origin) return undefined;
      return `habab-home-setup-scope:${encodeURIComponent(origin)}:${encodeURIComponent(user.id)}`;
    }

    setupScopeStorages() {
      if (typeof window === "undefined") return [];
      const storages = [];
      try { if (window.sessionStorage) storages.push(window.sessionStorage); } catch (_) {}
      try {
        if (window.localStorage && !storages.includes(window.localStorage)) storages.push(window.localStorage);
      } catch (_) {}
      return storages;
    }

    persistSetupBaseline() {
      const key = this.setupScopeStorageKey();
      if (!key || typeof this.sessionId !== "string" || !this.sessionId || this.sessionId.length > 128) return false;
      const value = JSON.stringify({
        version: 3,
        session_id: this.sessionId,
        interaction_ids: [...this.suppressedSetupInteractionIds]
          .filter((id) => typeof id === "string" && id.length > 0 && id.length <= 128).slice(0, 256),
        xiaomi_setup_id: typeof this.suppressedXiaomiSetupId === "string" && this.suppressedXiaomiSetupId.length <= 128
          ? this.suppressedXiaomiSetupId : null,
        fresh_chat_interaction_ids: [...this.freshChatSetupInteractionIds]
          .filter((id) => typeof id === "string" && id.length > 0 && id.length <= 128).slice(0, 256),
        fresh_chat_xiaomi_setup_id: typeof this.freshChatXiaomiSetupId === "string" && this.freshChatXiaomiSetupId.length <= 128
          ? this.freshChatXiaomiSetupId : null,
      });
      let stored = false;
      for (const storage of this.setupScopeStorages()) {
        try {
          storage.setItem(key, value);
          stored = true;
        } catch (_) {}
      }
      return stored;
    }

    restoreSetupBaseline(sessionId) {
      this.suppressedSetupInteractionIds = new Set();
      this.freshChatSetupInteractionIds = new Set();
      this.suppressedXiaomiSetupId = undefined;
      this.freshChatXiaomiSetupId = undefined;
      const key = this.setupScopeStorageKey();
      if (!key || typeof sessionId !== "string" || !sessionId) return false;
      for (const storage of this.setupScopeStorages()) {
        let raw;
        try { raw = storage.getItem(key); } catch (_) { continue; }
        if (typeof raw !== "string") continue;
        if (raw.length > 65536) {
          try { storage.removeItem(key); } catch (_) {}
          continue;
        }
        try {
          const saved = JSON.parse(raw);
          if (![1, 2, 3].includes(saved?.version) || saved.session_id !== sessionId ||
              !Array.isArray(saved.interaction_ids) || saved.interaction_ids.length > 256 ||
              (saved.version === 3 && (!Array.isArray(saved.fresh_chat_interaction_ids) ||
                saved.fresh_chat_interaction_ids.length > 256 ||
                (saved.fresh_chat_xiaomi_setup_id !== null && saved.fresh_chat_xiaomi_setup_id !== undefined &&
                  (typeof saved.fresh_chat_xiaomi_setup_id !== "string" || saved.fresh_chat_xiaomi_setup_id.length > 128)))) ||
              (saved.xiaomi_setup_id !== null && saved.xiaomi_setup_id !== undefined &&
                (typeof saved.xiaomi_setup_id !== "string" || saved.xiaomi_setup_id.length > 128))) continue;
          this.suppressedSetupInteractionIds = new Set(saved.interaction_ids.filter((id) =>
            typeof id === "string" && id.length > 0 && id.length <= 128
          ));
          this.suppressedXiaomiSetupId = saved.xiaomi_setup_id || undefined;
          this.freshChatSetupInteractionIds = new Set((saved.fresh_chat_interaction_ids || []).filter((id) =>
            typeof id === "string" && id.length > 0 && id.length <= 128
          ));
          this.freshChatXiaomiSetupId = saved.fresh_chat_xiaomi_setup_id || undefined;
          this.integrationCard?.setSuppressedInteractionIds?.(this.allSuppressedSetupInteractionIds());
          if (this.setupCard) this.setupCard.hidden = this.isXiaomiSetupSuppressed();
          return true;
        } catch (_) {
          try { storage.removeItem(key); } catch (_) {}
        }
      }
      this.integrationCard?.setSuppressedInteractionIds?.(this.allSuppressedSetupInteractionIds());
      return false;
    }

    captureSetupBaseline() {
      this.suppressedSetupInteractionIds = new Set(
        (this.integrationCard?.interactions || [])
          .filter(({phase}) => ["done", "abort", "error", "expired"].includes(phase))
          .map(({id}) => id).filter((id) => typeof id === "string")
      );
      this.freshChatSetupInteractionIds = new Set((this.integrationCard?.interactions || [])
        .map(({id}) => id).filter((id) => typeof id === "string" && id.length > 0));
      this.integrationCard?.setSuppressedInteractionIds?.(this.allSuppressedSetupInteractionIds());
      this.suppressedXiaomiSetupId = ["completed", "error"].includes(this.setupCard?.state?.phase)
        ? this.setupCard?.state?.setup_id : undefined;
      this.freshChatXiaomiSetupId = typeof this.setupCard?.state?.setup_id === "string"
        ? this.setupCard.state.setup_id : undefined;
      this.freshSetupSnapshotPending = true;
      this.freshSetupSnapshotArmed = false;
      this.freshXiaomiSnapshotPending = true;
      this.suppressExistingIntegrationSnapshot();
      if (this.setupCard) this.setupCard.hidden = this.isXiaomiSetupSuppressed();
      this.pendingSetupInteraction = undefined;
      const jump = this.q(".setup-jump");
      if (jump) jump.hidden = true;
      this.persistSetupBaseline();
    }

    suppressExistingIntegrationSnapshot() {
      if (!this.freshSetupSnapshotPending || this.freshSetupSnapshotArmed ||
          typeof this.integrationCard?.suppressExistingForFreshChat !== "function") return;
      const generation = this.setupLoadGeneration;
      const sessionId = this.sessionId;
      this.freshSetupSnapshotArmed = true;
      this.integrationCard.suppressExistingForFreshChat((ids) => {
        if (generation !== this.setupLoadGeneration || sessionId !== this.sessionId ||
            !Array.isArray(ids)) return;
        for (const id of ids) {
          if (typeof id === "string" && id.length > 0 && id.length <= 128) {
            this.freshChatSetupInteractionIds.add(id);
          }
        }
        this.freshSetupSnapshotPending = false;
        this.integrationCard?.setSuppressedInteractionIds?.(this.allSuppressedSetupInteractionIds());
        this.persistSetupBaseline();
      });
    }

    pruneActiveSetupSuppressions() {
      const phases = new Map((this.integrationCard?.interactions || []).map(({id, phase}) => [id, phase]));
      for (const id of this.suppressedSetupInteractionIds) {
        const phase = phases.get(id);
        if (phase && !["done", "abort", "error", "expired"].includes(phase)) {
          this.suppressedSetupInteractionIds.delete(id);
        }
      }
      this.integrationCard?.setSuppressedInteractionIds?.(this.allSuppressedSetupInteractionIds());
      this.persistSetupBaseline();
    }

    setContextAvailable(available) {
      if (this.demo) return;
      if (available) {
        const current = classifyAssistantContext(this.ownerId, locateMain(document));
        if (current.state !== "admin" || current.ownerId !== this.ownerId) return;
        const wasUnavailable = !this.contextAvailable || this.hidden || this.inert;
        if (!wasUnavailable) return;
        this.contextAvailable = true;
        this.hidden = false;
        this.inert = false;
        this.removeAttribute("data-context-unavailable");
        this.removeAttribute("aria-hidden");
        const dialog = this.shadowRoot?.querySelector?.("dialog");
        if (dialog?.open) this.setAssistantOpen(true);
        if (this.integrationCard) this.integrationCard.active = dialog?.open === true;
        if (this.isConnected && this.shadowRoot?.firstChild) this.controls();
        if (wasUnavailable && dialog?.open && !this.busy) this.startPendingAssist();
        return;
      }
      if (!this.contextAvailable && this.hidden) return;
      this.contextAvailable = false;
      this.cleanupVoice();
      this.nativeAssistConversation = undefined;
      this.pendingNativeAssistScope = undefined;
      this.assistPipelineId = undefined;
      this.assistPipelineChoiceSet = false;
      this.assistPipelines = undefined;
      this.preferredPipelineId = undefined;
      if (this.openParams) this.openParams.start_listening = false;
      if (this.integrationCard) this.integrationCard.active = false;
      const dialog = this.shadowRoot?.querySelector?.("dialog");
      if (dialog?.open) this.setAssistantOpen(false);
      const activeElement = this.shadowRoot?.activeElement;
      activeElement?.blur?.();
      this.inert = true;
      this.hidden = true;
      this.setAttribute("data-context-unavailable", "");
      this.setAttribute("aria-hidden", "true");
    }

    cleanupVoice() {
      this.voiceGeneration += 1;
      this.pendingVoiceText = undefined;
      this.pendingNativeAssistResponse = undefined;
      this.nativeAssistRun = false;
      this.nativeAssistPipeline = undefined;
      this.voiceReplyPipeline = undefined;
      const voice = this.voice;
      const wasVoiceBusy = this.voiceBusy;
      const hadNativeAssistBusy = this.nativeAssistBusyGeneration !== undefined;
      this.nativeAssistBusyGeneration = undefined;
      this.voice = undefined;
      this.voiceBusy = false;
      voice?.close();
      if ((wasVoiceBusy || hadNativeAssistBusy) && !this.running) this.busy = false;
      if (this.isConnected && this.shadowRoot?.firstChild) this.controls();
    }

    get t() { return copy[this.language || "en"]; }
    q(selector) { return this.shadowRoot.querySelector(selector); }

    setupFormReady(detail) {
      if (typeof detail?.interaction_id !== "string") return;
      if (this.suppressedSetupInteractionIds?.delete(detail.interaction_id)) {
        this.integrationCard?.setSuppressedInteractionIds?.(this.allSuppressedSetupInteractionIds());
        this.persistSetupBaseline();
      }
      if (this.freshChatSetupInteractionIds?.has(detail.interaction_id)) return;
      this.pendingSetupInteraction = detail.interaction_id;
      if (this.followSetupForms !== false && this.revealSetupForm()) return;
      const jump = this.q(".setup-jump");
      if (jump) jump.hidden = false;
    }

    revealSetupForm() {
      const shown = Boolean(this.integrationCard?.revealInteraction(this.pendingSetupInteraction));
      const jump = this.q(".setup-jump");
      if (jump) jump.hidden = true;
      if (shown) this.followSetupForms = true;
      return shown;
    }

    subscribeVoiceHistory() {
      const connection = !this.demo && this.isConnected && this.hass?.user?.is_admin
        ? this.hass.connection : undefined;
      const sessionId = this.sessionId;
      if (this.historyConnection === connection && this.historySessionId === sessionId) return;
      this.historyUnsubscribe?.();
      this.historyUnsubscribe = undefined;
      this.historyConnection = connection;
      this.historySessionId = sessionId;
      const attempt = this.historyAttempt = {};
      if (!connection) return;
      connection.subscribeMessage((result) => {
        if (this.historyAttempt !== attempt || result.session_id !== sessionId ||
            sessionId !== this.sessionId || !Array.isArray(result.messages)) return;
        this.messages = result.messages;
        this.draw();
      }, {type: "habab_home/realtime/subscribe"}).then((unsubscribe) => {
        if (this.historyAttempt === attempt) this.historyUnsubscribe = unsubscribe;
        else unsubscribe();
      }).catch(() => {
        if (this.historyAttempt === attempt) this.historyConnection = undefined;
      });
    }

    async loadSetup(afterTurn = false) {
      if (this.demo || !this.isCurrentOwnerContext()) return;
      const generation = this.setupLoadGeneration;
      const sessionId = this.sessionId;
      const isCurrent = () => generation === this.setupLoadGeneration && sessionId === this.sessionId &&
        this.isCurrentOwnerContext();
      try {
        await import("/habab-home/xiaomi-setup.js?v=5");
        await import("/habab-home/integration-setup.js?v=2");
        await import("/habab-home/automation-setup.js?v=6");
        if (!isCurrent()) return;
        if (!this.setupCard) {
          this.setupCard = document.createElement("habab-xiaomi-setup");
          this.setupCard.addEventListener("habab-setup-dismiss", () => {
            const phase = this.setupCard?.state?.phase;
            if (!this.isCurrentOwnerContext() || !["completed", "error"].includes(phase)) return;
            this.setXiaomiSetupDismissed(true);
            this.updateXiaomiSetupPresentation(phase);
          });
          this.setupCard.addEventListener("habab-setup-verify", () => {
            if (this.busy) return;
            this.q(".composer-textarea").value = this.language === "ru"
              ? "Проверь подключение Xiaomi Home и доступные устройства реальными командами. Не управляй устройствами."
              : "Check Xiaomi Home and available devices using actual commands. Do not control devices.";
            this.send();
          });
          this.setupCard.addEventListener("habab-setup-state", ({detail}) => {
            const phase = detail?.phase;
            if (typeof phase === "string") this.updateXiaomiSetupPresentation(phase);
            if (phase === "restarting") {
              this.setupRestarting = true;
              this.controls();
            } else if (this.setupRestarting) {
              this.setupRestarting = false;
              this.controls();
            }
          });
          this.setupCard.addEventListener("habab-open-devices", async () => {
            if (!this.isCurrentOwnerContext()) return;
            this.status(this.t.openingDevices);
            try {
              const {openDevices} = await import("/habab-home/device-dashboard.js?v=2");
              const result = await openDevices(this.hass);
              this.status(result.count ? this.t.devicesReady : this.t.noVacuumDevices);
              this.setMobileExpanded(false);
            } catch (_) {
              this.status(this.t.devicesFailed, true);
            }
          });
        }
        if (!this.integrationCard) {
          this.integrationCard = document.createElement("habab-integration-setup");
          this.integrationCard.addEventListener("habab-setup-form-ready", ({detail}) => this.setupFormReady(detail));
          this.integrationCard.addEventListener("habab-setup-verify", ({detail}) => {
            const prompt = integrationVerificationPrompt(detail, this.language);
            if (this.busy || !prompt) return;
            this.q(".composer-textarea").value = prompt;
            this.send();
          });
        }
        this.suppressExistingIntegrationSnapshot();
        this.setupCard.hass = this.hass;
        if (!this.getXiaomiSetupDismissed()) mountStableElement(this.q("#setup-cards"), this.setupCard);
        this.setupCard.hidden = this.isXiaomiSetupSuppressed();
        await this.setupCard.load();
        if (!isCurrent()) return;
        this.updateXiaomiSetupPresentation(this.setupCard.state?.phase || "idle");
        this.freshXiaomiSnapshotPending = false;
        this.integrationCard.hass = this.hass;
        this.integrationCard.active = this.q("dialog").open;
        mountStableElement(this.q("#setup-cards"), this.integrationCard);
        this.integrationCard.setSuppressedInteractionIds?.(this.allSuppressedSetupInteractionIds());
        if (afterTurn && typeof this.integrationCard.refresh === "function") await this.integrationCard.refresh();
        else await this.integrationCard.load();
        if (!isCurrent()) return;
        if (this.freshSetupSnapshotPending && this.integrationCard.statusError) this.freshSetupSnapshotArmed = false;
        this.pruneActiveSetupSuppressions();
        if (!this.automationCard) {
          this.automationCard = document.createElement("habab-automation-setup");
          this.automationCard.isCurrentContext = () => this.isCurrentOwnerContext() && this.sessionId === this.automationCard.sessionId;
        }
        this.automationCard.hass = this.hass;
        this.automationCard.sessionId = this.sessionId;
        mountStableElement(this.q("#setup-cards"), this.automationCard);
        await this.automationCard.refresh();
        if (!isCurrent()) return;
      } catch (_) { if (isCurrent()) this.status(this.t.setup_ui_unavailable, true); }
    }

    connectedCallback() {
      if (!this.demo && !this.reloadStateProvider && typeof window !== "undefined" &&
          typeof window.haRegisterDirtyStateProvider === "function") {
        this.reloadStateProvider = window.haRegisterDirtyStateProvider(this);
      }
      this.syncReloadState();
      this.subscribeVoiceHistory();
      if (this.shadowRoot.firstChild) {
        this.installLayoutLifecycle();
        if (this.q("dialog").open) this.setAssistantOpen(true);
        if (visibleTextMessages(this.messages).some((message) => message.role === "assistant")) {
          this.draw({preserveScroll: true});
        }
        return;
      }
      this.language = this.demo || this.hass?.locale.language.startsWith("ru") ? "ru" : "en";
      const t = this.t;
      this.shadowRoot.innerHTML = `<style>
        :host{position:fixed;z-index:10;inset-inline-end:max(16px,env(safe-area-inset-right));bottom:calc(16px + var(--safe-area-inset-bottom,0px) + 72px);display:block;white-space:normal;pointer-events:none;color:var(--primary-text-color,#212121);font:14px/1.55 var(--paper-font-body1_-_font-family,Roboto,system-ui,sans-serif)}
        :host([data-context-unavailable]){display:none !important;pointer-events:none !important}
        *{box-sizing:border-box}button,textarea,select{font:inherit}button{cursor:pointer;color:inherit;background:transparent;border:0;min-height:44px;border-radius:8px;padding:8px 12px}button:hover{background:var(--secondary-background-color,#f0f2f4)}button:disabled,select:disabled{opacity:.5;cursor:default}.voice-mode{max-width:104px;min-height:44px;padding:5px 6px;border:1px solid var(--divider-color,#c8cccf);border-radius:8px;color:inherit;background:var(--card-background-color,#fff);font-size:12px}
        :is(button,textarea,a):focus-visible{outline:3px solid var(--primary-color,#007a9e);outline-offset:2px}::selection{background:var(--primary-color,#007a9e);color:#fff}
        .icon{width:56px;height:56px;padding:16px;display:grid;place-items:center}.launcher{position:relative;z-index:1;pointer-events:auto;border-radius:50%;background:var(--primary-color,#007a9e);color:var(--text-primary-color,#fff);box-shadow:0 3px 12px #0003}.launcher:hover{background:var(--dark-primary-color,#006686)}svg{width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
        dialog{pointer-events:auto;position:fixed;inset:auto max(16px,env(safe-area-inset-right)) max(84px,calc(env(safe-area-inset-bottom) + 72px)) auto;color:var(--primary-text-color,#212121);background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#e3e6e8);border-radius:16px;padding:0;width:min(420px,calc(100vw - 32px));height:min(680px,calc(100dvh - 120px));max-width:none;max-height:none;margin:0;box-shadow:0 12px 36px #0003}
        dialog::backdrop{background:transparent}.layout{height:100%;display:flex;flex-direction:column;min-width:0}header{display:flex;align-items:center;gap:8px;padding:12px 16px;border-block-end:1px solid var(--divider-color,#e3e6e8)}h2{font-size:18px;font-weight:500;margin:0;flex:1}.fresh{font-size:13px}.collapse{display:none}
        #log{flex:1;min-height:0;overflow:auto;padding:24px;scrollbar-color:var(--secondary-text-color,#6b7279) transparent;scrollbar-width:thin}.empty{height:100%;width:100%;display:flex;flex-direction:column;justify-content:center;max-width:440px;margin:auto}.empty h3{font-size:24px;line-height:1.3;font-weight:500;margin:0 0 16px}
        .tool-calls{margin-block:12px 4px;border:1px solid var(--divider-color,#c8cccf);border-radius:10px;padding:8px 12px;max-inline-size:100%;font-size:13px;white-space:normal}.tool-calls summary{cursor:pointer;min-height:32px;align-content:center;overflow-wrap:anywhere}.tool-calls summary:focus-visible{outline:2px solid var(--primary-color,#007a9e);outline-offset:2px}.tool-call{margin-block-start:8px;padding-block-start:4px;border-block-start:1px solid var(--divider-color,#c8cccf)}.tool-calls .tool-caption{margin-block:8px 4px;color:var(--secondary-text-color,#5f6368);font-size:12px}.tool-calls pre{white-space:pre-wrap;overflow-wrap:anywhere;overflow:auto;max-block-size:18rem;max-inline-size:100%;margin:0;padding:8px;border-radius:6px;background:var(--secondary-background-color,#f0f2f4);font:12px/1.5 ui-monospace,monospace}.tool-calls a{color:var(--primary-color,#007a9e)}
        .message{white-space:pre-wrap;overflow-wrap:anywhere;max-width:65ch;margin-block-end:24px}.message.user{margin-inline-start:auto;background:var(--secondary-background-color,#f0f2f4);border-radius:16px;padding:12px 16px;width:fit-content;max-width:85%}.message.assistant{padding-inline:4px}.name{font-size:12px;font-weight:500;color:var(--secondary-text-color,#5f6368);margin-block-end:5px}.message p{margin:0}ha-markdown{display:block;white-space:normal;overflow:auto;max-width:100%}.answer-feedback{display:flex;align-items:center;gap:6px;margin-block-start:8px;color:var(--secondary-text-color,#5f6368);font-size:12px}.answer-feedback button{min-width:40px;min-height:36px;padding:4px 8px;border:1px solid var(--divider-color,#c8cccf);border-radius:18px}.answer-feedback button[aria-pressed="true"]{border-color:var(--primary-color,#007a9e);background:var(--secondary-background-color,#f0f2f4)}.answer-feedback [role="status"]{margin:0;white-space:normal}
        .quick-replies{display:flex;flex-wrap:wrap;gap:8px;margin-block-start:16px}.quick-reply{border:1px solid var(--divider-color,#c8cccf);border-radius:18px;padding:7px 12px;min-height:40px;background:var(--card-background-color,#fff)}.quick-reply:hover{border-color:var(--primary-color,#007a9e);background:var(--secondary-background-color,#f0f2f4)}
        footer{padding:12px 20px 20px}.assist-pipeline-control{display:block;margin:0 0 8px;font-size:12px;color:var(--secondary-text-color,#5f6368)}.assist-pipeline{display:block;width:100%;min-height:40px;margin-block-start:4px;padding:6px;border:1px solid var(--divider-color,#c8cccf);border-radius:8px;color:var(--primary-text-color,#212121);background:var(--card-background-color,#fff)}#voice-mode-help{font-size:12px;color:var(--secondary-text-color,#5f6368);margin:0 0 8px}#status{font-size:13px;color:var(--secondary-text-color,#5f6368);margin:0 0 10px;white-space:pre-wrap;overflow-wrap:anywhere}#status.error{color:var(--error-color,#b3261e)}.recovery{display:flex;gap:12px;align-items:center;margin-block-end:12px}a{color:var(--primary-color,#007a9e);text-underline-offset:3px}.recovery button{text-decoration:underline}
        form{border:1px solid var(--secondary-text-color,#81868b);border-radius:14px;padding:8px;display:flex;gap:8px;align-items:flex-end}textarea{color:inherit;caret-color:var(--primary-color,#007a9e);background:transparent;border:0;resize:none;min-width:0;flex:1;min-height:64px;max-height:128px;padding:8px;line-height:1.5}textarea::placeholder{color:var(--secondary-text-color,#5f6368);opacity:1}textarea:focus-visible{outline-offset:0}.send{color:var(--text-primary-color,#fff);background:var(--primary-color,#007a9e)}.send:hover{background:var(--dark-primary-color,#006686)}[hidden]{display:none!important}
        :host([data-assistant-open]){inset:0;z-index:10;pointer-events:none}:host([data-assistant-open]) .launcher{display:none}:host([data-assistant-open]) dialog{inset:0 0 0 auto;width:var(--habab-assistant-chat-width,50vw);height:100dvh;border-radius:0;border-block:0;border-inline-end:0;box-shadow:-8px 0 28px #0002}:host([data-assistant-open]) dialog:not([open]){display:none}:host([data-assistant-open]) dialog::backdrop{background:transparent}
        @media(max-width:720px){:host{inset:0;z-index:1000}:host([data-assistant-open]) dialog{inset-inline:0;inset-block-start:50dvh;inset-block-end:0;width:100vw;height:50dvh;max-height:50dvh;border-radius:0}:host([data-assistant-open][data-assistant-mobile-expanded]) dialog{inset-block-start:var(--assistant-viewport-offset-top,0px);height:var(--assistant-viewport-height,100dvh);max-height:var(--assistant-viewport-height,100dvh)}:host([data-assistant-mobile-expanded]) .collapse{display:grid}:host(:not([data-assistant-open])) .launcher{position:fixed;inset-inline-end:max(16px,env(safe-area-inset-right));bottom:calc(84px + var(--safe-area-inset-bottom,0px) + 72px)}}
        @media(max-width:720px){header{padding:8px;gap:4px}header .icon{width:44px;height:44px;padding:10px}h2{font-size:16px}.fresh{font-size:12px;padding-inline:6px}#log{padding:16px}.empty h3{font-size:20px;margin-block-end:12px}.quick-replies{margin-block-start:8px}}
        @media(prefers-reduced-motion:no-preference){dialog[open]{animation:arrive 160ms cubic-bezier(.16,1,.3,1)}@keyframes arrive{from{transform:translateY(10px);opacity:.7}to{transform:translateY(0);opacity:1}}}
      </style>
      <style>
        .answer-feedback{flex-wrap:wrap}
        .answer-feedback-editor{display:grid;gap:8px;inline-size:100%;margin-block-start:8px}
        .answer-feedback-editor label{font-size:12px;color:var(--secondary-text-color,#5f6368)}
        .answer-feedback-editor textarea{display:block;inline-size:100%;min-block-size:72px;max-block-size:160px;flex:none;border:1px solid var(--divider-color,#c8cccf);border-radius:8px;background:var(--card-background-color,#fff)}
        .answer-feedback-actions{display:flex;justify-content:flex-end;gap:8px}
        .answer-feedback-send{background:var(--primary-color,#007a9e);color:var(--text-primary-color,#fff)}
      </style>
      <button class="icon launcher" type="button" title="${t.open}" aria-label="${t.open}" aria-haspopup="dialog"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11.5a7.5 7.5 0 0 1-7.5 7.5H5l-3 3V11.5A7.5 7.5 0 0 1 9.5 4h3A7.5 7.5 0 0 1 20 11.5Z"/><path d="M7 10h8M7 14h5"/></svg></button>
        <dialog aria-labelledby="chat-title"><div class="layout"><header><h2 id="chat-title">${t.title}</h2><select class="voice-mode" aria-label="${t.voiceModeLabel}" title="${t.nativeAssistTitle}"><option value="habab">${t.voiceModeHabab}</option><option value="assist">${t.voiceModeAssist}</option></select><button class="mic icon" type="button" aria-label="${t.mic}" title="${t.mic}"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3m-4 0h8"/></svg></button><button class="collapse icon" type="button" aria-label="${t.collapseChat}" title="${t.collapseChat}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></button><button class="fresh" type="button">${t.fresh}</button><button class="icon close" type="button" aria-label="${t.close}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button></header>
      <div id="log"><div id="messages" role="log" aria-live="polite" aria-label="${t.title}"></div><div id="setup-cards"></div></div><footer><label class="assist-pipeline-control" hidden>${t.assistPipelineLabel}<select class="assist-pipeline"><option value="">${t.assistPipelineDefault}</option></select></label><p id="voice-mode-help" class="help" hidden></p><p id="status" role="status"></p><button class="setup-jump" type="button" hidden>${t.showSetupForm}</button><div class="recovery" hidden><a href="/habab-ai">${t.settings}</a><button type="button" class="retry">${t.retry}</button></div>
      <form class="composer-form"><textarea class="composer-textarea" aria-label="${t.placeholder}" placeholder="${t.placeholder}" maxlength="4000" rows="2"></textarea><button class="send icon" type="submit" aria-label="${t.send}" title="${t.send}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg></button><button class="stop" type="button" hidden>${t.stop}</button></form></footer></div></dialog>`;
      this.q(".launcher").onclick = () => this.open();
      this.q(".close").onclick = () => this.q("dialog").close();
      this.q("dialog").addEventListener("close", () => {
        this.setAssistantOpen(false);
        this.onClose();
      });
      this.q(".collapse").onclick = () => this.collapseMobileChat();
      this.q(".voice-mode").value = this.voiceMode;
      this.q(".voice-mode").addEventListener("change", (event) => {
        this.voiceMode = event.currentTarget.value === "assist" ? "assist" : "habab";
        this.updatePipelineControl();
        if (this.voiceMode === "assist") void this.loadPipelineOptions();
        this.updateVoiceModeHelp();
        this.controls();
      });
      this.q(".assist-pipeline").addEventListener("change", (event) => {
        this.assistPipelineChoiceSet = true;
        this.assistPipelineId = event.currentTarget.value || undefined;
        if (this.openParams) this.openParams.invalid_pipeline_id = false;
        this.status("");
      });
      this.q(".mic").onclick = () => this.voiceBusy ? this.finishVoice()
        : this.voiceMode === "assist"
          ? this.startVoice(this.assistPipelineChoiceSet ? this.assistPipelineId || "last_used" : this.openParams?.pipeline_id || "last_used", this.openParams?.invalid_pipeline_id === true, true)
          : this.startVoice("last_used");
      this.q(".fresh").onclick = () => this.connect(true);
      this.q(".retry").onclick = () => this.connect();
      this.q(".setup-jump").onclick = () => this.revealSetupForm();
      const log = this.q("#log");
      log.addEventListener("wheel", (event) => {
        if (event.deltaY < 0) this.followSetupForms = false;
      }, {passive: true});
      log.addEventListener("scroll", () => {
        if (log.scrollHeight - log.clientHeight - log.scrollTop < 96) this.followSetupForms = true;
      }, {passive: true});
      let touchY;
      log.addEventListener("touchstart", (event) => {touchY = event.touches[0]?.clientY;}, {passive: true});
      log.addEventListener("touchmove", (event) => {
        const next = event.touches[0]?.clientY;
        if (next > touchY) this.followSetupForms = false;
        touchY = next;
      }, {passive: true});
      log.addEventListener("keydown", (event) => {
        if (["PageUp", "Home", "ArrowUp"].includes(event.key) &&
            !event.composedPath().some(isEditableFocusTarget)) this.followSetupForms = false;
      });
      this.q(".composer-form").onsubmit = (event) => {event.preventDefault(); this.send();};
      this.q(".composer-textarea").oninput = () => this.syncReloadState();
      this.q(".composer-textarea").onkeydown = (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {event.preventDefault(); this.send();}
      };
      this.shadowRoot.addEventListener("focusin", (event) => {
        if (!this.q("dialog").open) return;
        const path = event.composedPath();
        const target = path.find(isEditableFocusTarget);
        if (!target) return;
        if (target === this.q(".composer-textarea") || target?.classList?.contains("answer-feedback-comment") ||
            [...(this.integrationCard?.cards?.values?.() || [])].some((card) => card.form && path.includes(card.form))) {
          this.setMobileExpanded(true);
        }
      });
      this.q(".stop").onclick = () => this.stop();
      this.shadowRoot.addEventListener("click", (event) => {
        const target = event.composedPath().find((item) => item?.classList);
        if (target?.classList.contains("quick-reply")) this.chooseQuickReply(target.dataset.prompt, target.dataset.setupKind);
      });
      this.installLayoutLifecycle();
      this.draw();
      if (this.autoOpenOnConnect && !this.autoOpenStarted) {
        this.autoOpenStarted = true;
        this.open({}, null, {focus: false});
      }
    }

    installLayoutLifecycle() {
      if (this.layoutLifecycleInstalled) return;
      this.layoutLifecycleInstalled = true;
      this.assistantViewport = window.visualViewport;
      this.onAssistantViewportChange = () => this.updateAssistantViewport();
      window.addEventListener("resize", this.onAssistantViewportChange);
      this.assistantViewport?.addEventListener("resize", this.onAssistantViewportChange);
      this.assistantViewport?.addEventListener("scroll", this.onAssistantViewportChange);
      this.updateAssistantViewport();
    }

    disconnectedCallback() {
      this.markdownUpgradeLifecycle += 1;
      this.markdownUpgradePending = false;
      this.drawGeneration += 1;
      this.cleanupVoice();
      this.reloadStateProvider?.unregister();
      this.reloadStateProvider = undefined;
      if (this.integrationCard) this.integrationCard.active = false;
      this.historyConnection = undefined;
      this.historySessionId = undefined;
      this.historyAttempt = undefined;
      this.historyUnsubscribe?.();
      this.historyUnsubscribe = undefined;
      window.removeEventListener("resize", this.onAssistantViewportChange);
      this.assistantViewport?.removeEventListener("resize", this.onAssistantViewportChange);
      this.assistantViewport?.removeEventListener("scroll", this.onAssistantViewportChange);
      this.assistantViewport = undefined;
      this.layoutLifecycleInstalled = false;
      this.setAssistantOpen(false);
    }

    dashboardRoot() {
      return document.querySelector(this.demo ? "body > ha-demo" : "body > home-assistant");
    }

    setAssistantOpen(open) {
      if (open) {
        installAssistantSplitStyles();
        document.documentElement.dataset.hababAssistantOpen = "true";
        this.setAttribute("data-assistant-open", "");
      } else {
        this.setMobileExpanded(false);
        delete document.documentElement.dataset.hababAssistantOpen;
        this.removeAttribute("data-assistant-open");
      }
    }

    setMobileExpanded(expanded) {
      const mobile = matchMedia("(max-width: 720px)").matches;
      const shouldExpand = expanded === true && mobile && this.q("dialog")?.open === true;
      if (shouldExpand) {
        const root = this.dashboardRoot();
        if (!this.mobileExpanded || root !== this.mobileInertRoot) {
          this.restoreDashboardInert();
          if (root && !root.inert) {
            this.mobileInertOriginal = root.inert;
            root.inert = true;
            this.mobileInertRoot = root;
          }
        }
      } else {
        this.restoreDashboardInert();
      }
      this.mobileExpanded = shouldExpand;
      this.toggleAttribute("data-assistant-mobile-expanded", this.mobileExpanded);
      if (this.mobileExpanded) document.documentElement.dataset.hababAssistantExpanded = "true";
      else delete document.documentElement.dataset.hababAssistantExpanded;
    }

    restoreDashboardInert() {
      if (this.mobileInertRoot?.inert === true) this.mobileInertRoot.inert = this.mobileInertOriginal;
      this.mobileInertRoot = undefined;
      this.mobileInertOriginal = undefined;
    }

    updateAssistantViewport() {
      if (this.assistantViewport) {
        this.style.setProperty("--assistant-viewport-height", `${Math.max(240, this.assistantViewport.height)}px`);
        this.style.setProperty("--assistant-viewport-offset-top", `${Math.max(0, this.assistantViewport.offsetTop)}px`);
      }
      if (!matchMedia("(max-width: 720px)").matches && this.mobileExpanded) this.setMobileExpanded(false);
    }

    collapseMobileChat() {
      this.q(".composer-textarea").blur();
      this.setMobileExpanded(false);
      this.q(".close").focus({preventScroll: true});
    }

    chooseQuickReply(prompt, setupKind) {
      if (typeof prompt !== "string" || this.busy || !this.sessionId) return;
      if (setupKind === "xiaomi") {
        this.suppressedXiaomiSetupId = undefined;
        if (this.setupCard) this.setupCard.hidden = false;
        this.setXiaomiSetupDismissed(false);
        void this.loadSetup();
      }
      const textarea = this.q(".composer-textarea");
      const draft = textarea.value;
      if (draft.trim()) {
        textarea.value = appendQuickReplyToDraft(draft, prompt);
        this.syncReloadState();
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
      this.q(".collapse").title = t.collapseChat;
      this.q(".collapse").setAttribute("aria-label", t.collapseChat);
      this.q(".mic").title = t.mic;
      this.q(".mic").setAttribute("aria-label", t.mic);
      const voiceMode = this.q(".voice-mode");
      voiceMode.setAttribute("aria-label", t.voiceModeLabel);
      voiceMode.title = t.nativeAssistTitle;
      voiceMode.options[0].textContent = t.voiceModeHabab;
      voiceMode.options[1].textContent = t.voiceModeAssist;
      const pipelineSelect = this.q(".assist-pipeline");
      pipelineSelect.options[0].textContent = t.assistPipelineDefault;
      this.q(".assist-pipeline-control").firstChild.textContent = t.assistPipelineLabel;
      this.updatePipelineControl();
      this.updateVoiceModeHelp();
      this.q("#messages").setAttribute("aria-label", t.title);
      const textarea = this.q(".composer-textarea");
      textarea.placeholder = t.placeholder;
      textarea.setAttribute("aria-label", t.placeholder);
      this.q(".recovery a").textContent = t.settings;
      this.q(".retry").textContent = t.retry;
      if (this.q(".setup-jump")) this.q(".setup-jump").textContent = t.showSetupForm;
      this.q(".send").title = t.send;
      this.q(".send").setAttribute("aria-label", t.send);
      this.q(".stop").textContent = t.stop;
      this.draw();
    }

    open(params = {}, opener = null, options = {}) {
      if (!this.isConnected || !this.isCurrentOwnerContext() || !params || typeof params !== "object") return false;
      const mobile = matchMedia("(max-width: 720px)").matches;
      const focusComposer = options?.focus !== false && !mobile;
      this.focusComposerOnOpen = focusComposer;
      this.openParams = {
        ...params,
        pipeline_id: typeof params.pipeline_id === "string" ? params.pipeline_id : "last_used",
        invalid_pipeline_id: params.pipeline_id !== undefined && (typeof params.pipeline_id !== "string" || !params.pipeline_id || params.pipeline_id.length > 256),
        start_listening: params.start_listening === true,
        initial_prompt: typeof params.prompt === "string" && params.prompt.length <= 4000 ? params.prompt : undefined,
        submit_prompt: params.submit === true && typeof params.prompt === "string" && params.prompt.length <= 4000,
      };
      if (this.openParams.initial_prompt !== undefined) this.q(".composer-textarea").value = this.openParams.initial_prompt;
      this.syncReloadState();
      this.returnFocus = opener || this.q(".launcher");
      this.setAssistantOpen(true);
      const dialog = this.q("dialog");
      if (!dialog.open) dialog.show();
      if (this.voiceMode === "assist") void this.loadPipelineOptions();
      if (this.integrationCard) this.integrationCard.active = true;
      if (focusComposer) this.q(".composer-textarea").focus({preventScroll: true});
      else this.q(".close").focus({preventScroll: true});
      if (!this.sessionId && !this.busy) void this.connect();
      else if (this.sessionId && !this.busy) this.startPendingAssist();
      return true;
    }

    onClose() {
      if (this.integrationCard) this.integrationCard.active = false;
      this.cleanupVoice();
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
      this.q(".composer-textarea").disabled = !this.sessionId || this.busy || this.setupRestarting;
      this.q(".voice-mode").disabled = this.demo || !this.sessionId || this.busy || this.setupRestarting;
      this.q(".voice-mode").hidden = this.demo;
      this.q(".mic").disabled = this.demo || !this.sessionId || (this.busy && !this.voiceBusy) || this.setupRestarting;
      this.q(".mic").hidden = this.demo;
      this.q(".assist-pipeline").disabled = this.busy || this.setupRestarting;
      const micLabel = this.voiceBusy ? this.t.stopVoice : this.voiceMode === "assist" ? this.t.nativeAssistMic : this.t.mic;
      this.q(".mic").title = micLabel;
      this.q(".mic").setAttribute("aria-label", micLabel);
      this.q(".send").disabled = !this.sessionId || this.busy || this.setupRestarting;
      this.q(".send").hidden = false;
      this.q(".stop").hidden = !this.running || this.demo;
      this.q(".fresh").disabled = this.busy || !this.sessionId;
      this.q(".retry").disabled = this.busy;
      for (const button of this.shadowRoot.querySelectorAll(".quick-reply")) button.disabled = this.busy || !this.sessionId;
      this.syncReloadState();
    }

    updateVoiceModeHelp() {
      const help = this.q("#voice-mode-help");
      if (!help) return;
      help.textContent = this.voiceMode === "assist" ? this.t.nativeAssistTitle : "";
      help.hidden = this.voiceMode !== "assist";
    }

    updatePipelineControl() {
      const control = this.q(".assist-pipeline-control");
      if (control) control.hidden = this.voiceMode !== "assist";
    }

    async loadPipelineOptions() {
      const select = this.q(".assist-pipeline");
      if (!select || this.demo) return;
      try {
        const pipelines = await this.pipelines(true);
        if (!this.isCurrentOwnerContext()) return;
        select.replaceChildren(new Option(this.t.assistPipelineDefault, ""));
        for (const pipeline of pipelines) {
          if (typeof pipeline?.id !== "string" || !pipeline.id || typeof pipeline.name !== "string") continue;
          select.add(new Option(pipeline.name, pipeline.id));
        }
        const displayId = this.assistPipelineChoiceSet ? this.assistPipelineId
          : this.openParams?.pipeline_id !== "last_used" ? this.openParams?.pipeline_id : undefined;
        if (displayId && pipelines.some((pipeline) => pipeline?.id === displayId)) {
          select.value = displayId;
        } else if (displayId) {
          select.add(new Option(this.t.assistPipelineUnavailable, displayId));
          select.value = displayId;
          this.status(this.t.assistPipelineUnavailable, true);
        } else {
          select.value = "";
        }
      } catch (_) {
        if (this.isCurrentOwnerContext()) this.status(this.t.assistPipelinesFailed, true);
      }
    }

    syncReloadState() {
      if (this.demo) return;
      if (!this.reloadStateProvider && typeof window !== "undefined" &&
          typeof window.haRegisterDirtyStateProvider === "function") {
        this.reloadStateProvider = window.haRegisterDirtyStateProvider(this);
      }
      const textarea = this.shadowRoot?.querySelector?.(".composer-textarea");
      this.reloadStateProvider?.setDirty(shouldBlockFrontendReload({
        busy: this.busy,
        voiceBusy: this.voiceBusy,
        activeVoice: this.voice !== undefined,
        pendingVoiceText: this.pendingVoiceText,
        draft: textarea?.value,
      }));
    }

    async connect(fresh = false) {
      if (this.busy || !this.isCurrentOwnerContext()) return;
      if (fresh) {
        this.setupLoadGeneration += 1;
        this.assistPipelineId = undefined;
        this.assistPipelineChoiceSet = false;
        if (this.openParams) {
          this.openParams.pipeline_id = "last_used";
          this.openParams.invalid_pipeline_id = false;
        }
        this.q(".assist-pipeline").value = "";
      }
      this.busy = true;
      this.status(this.t.loading);
      this.controls();
      const connectionOwner = this.ownerId;
      try {
        const result = this.demo
          ? await this.demoRequest(fresh ? "/reset" : "", fresh ? "POST" : "GET")
          : await requestAssistantSession(this.hass, {
            fresh, isCurrent: () => this.ownerId === connectionOwner && this.isCurrentOwnerContext(),
            onActivating: () => this.status(this.t.activatingBeta),
          });
        this.sessionId = result.session_id;
        this.subscribeVoiceHistory();
        if (this.automationCard) this.automationCard.sessionId = this.sessionId;
        if (fresh) this.captureSetupBaseline();
        else this.restoreSetupBaseline(this.sessionId);
        this.messages = result.messages;
        this.draw();
        this.status("");
        await this.loadSetup();
      } catch (error) { this.status(this.t[error.code] || this.t.failed, true); }
      finally {
        this.busy = false; this.controls();
        if (this.q("dialog").open && this.focusComposerOnOpen && !matchMedia("(max-width: 720px)").matches) this.q(".composer-textarea").focus({preventScroll: true});
        this.startPendingAssist();
      }
    }

    async demoRequest(suffix = "", method = "GET", text) {
      const response = await fetch(`/api/ha-demo/chat${suffix}`, {
        method, credentials: "same-origin", headers: {"Content-Type": "application/json"},
        ...(text !== undefined ? {body: JSON.stringify({text})} : {}),
      });
      const result = await response.json();
      if (!response.ok) throw {code: result.error};
      return {session_id: "public-demo", messages: result.messages.map(item => ({role: item.role, text: item.content}))};
    }

    startPendingAssist() {
      if (!this.sessionId || this.busy || !this.q("dialog").open || !this.openParams || !this.isCurrentOwnerContext()) return;
      if (this.openParams.submit_prompt && this.openParams.initial_prompt) {
        const prompt = this.openParams.initial_prompt;
        this.openParams.submit_prompt = false;
        void this.send(prompt, {clearDraft: true});
        return;
      }
      if (this.demo) return;
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

    async pipelines(refresh = false) {
      if (!refresh && this.assistPipelines) return this.assistPipelines;
      const ownerId = this.ownerId;
      const result = await this.hass.callWS({type: "assist_pipeline/pipeline/list"});
      if (ownerId !== this.ownerId || !this.isCurrentOwnerContext()) throw {code: "context_unavailable"};
      if (!Array.isArray(result?.pipelines)) throw {code: "invalid_pipeline_list"};
      this.assistPipelines = result.pipelines;
      this.preferredPipelineId = result.preferred_pipeline;
      return this.assistPipelines;
    }

    async startVoice(requestedId = "last_used", invalidId = false, nativeAssist = false) {
      if (this.demo || this.busy || !this.sessionId || !this.isCurrentOwnerContext()) return;
      const generation = ++this.voiceGeneration;
      this.voiceBusy = true;
      this.busy = true;
      this.pendingVoiceText = undefined;
      this.pendingNativeAssistResponse = undefined;
      this.nativeAssistEndCue = Promise.resolve();
      this.nativeAssistRun = nativeAssist;
      this.controls();
      try {
        let route;
        if (!nativeAssist) {
          const capability = await this.realtimeRequest("session", "GET");
          if (generation !== this.voiceGeneration || !this.q("dialog").open || !this.isCurrentOwnerContext()) return;
          route = selectBrowserVoiceRoute(capability);
          if (route === "habab_browser_v1") {
            await this.startBrowserVoice(capability, generation);
            return;
          }
          if (route !== "ha_assist_pipeline") throw {code: "browserVoiceUnavailable"};
        }
        if (invalidId) throw {code: "noAssistPipeline"};
        const explicitPipelineId = nativeAssist && requestedId !== "last_used" && requestedId !== "preferred"
          ? requestedId : undefined;
        const pipelines = await this.pipelines(!!explicitPipelineId);
        if (generation !== this.voiceGeneration || !this.q("dialog").open) return;
        let lastUsedId;
        try { lastUsedId = JSON.parse(localStorage.getItem("AssistPipelineId")); } catch (_) { lastUsedId = undefined; }
        const pipeline = chooseAssistPipeline(requestedId, pipelines, this.preferredPipelineId, lastUsedId);
        if (!pipeline) throw {code: explicitPipelineId ? "assistPipelineUnavailable" : "noAssistPipeline"};
        if (!pipeline.stt_engine) throw {code: "speechRequired"};
        if (nativeAssist && !pipeline.conversation_engine) throw {code: "noAssistPipeline"};
        this.voiceReplyPipeline = pipeline.tts_engine ? pipeline : undefined;
        this.nativeAssistPipeline = nativeAssist ? pipeline : undefined;
        const nativeScope = JSON.stringify([this.hass?.user?.id, this.sessionId, pipeline.id]);
        this.pendingNativeAssistScope = nativeScope;
        const conversationId = nativeAssist && this.nativeAssistConversation?.scope === nativeScope
          ? this.nativeAssistConversation.conversationId : undefined;
        await playVoiceCue("start");
        if (generation !== this.voiceGeneration || !this.q("dialog").open || !this.isCurrentOwnerContext()) return;
        this.status(this.t.listening);
        this.voice = new AssistVoice(this.hass, pipeline, (event) => {
          if (generation !== this.voiceGeneration || !this.isCurrentOwnerContext()) return;
          this.onVoiceEvent(event);
        }, nativeAssist ? {runConversation: true, conversationId} : undefined);
        await this.voice.start();
      } catch (error) {
        if (generation !== this.voiceGeneration) return;
        this.voice?.close();
        this.voice = undefined;
        this.voiceBusy = false;
        this.busy = false;
        this.nativeAssistRun = false;
        this.nativeAssistPipeline = undefined;
        this.pendingNativeAssistResponse = undefined;
        this.voiceReplyPipeline = undefined;
        const voiceStrings = strings[this.language || "en"];
        const code = typeof error?.code === "string" ? error.code : "failed";
        this.status(voiceStrings[code] || this.t[code] || this.t.failed, true);
        this.controls();
      }
    }

    async realtimeRequest(action, method, body, signal) {
      const options = {method, cache: "no-store", signal};
      if (body !== undefined) {
        options.headers = {"Content-Type": "application/json"};
        options.body = JSON.stringify(body);
      }
      const response = await this.hass.fetchWithAuth(`/api/habab_home/realtime/${action}`, options);
      let result;
      try { result = await response.json(); } catch (_) { result = undefined; }
      if (!response.ok) {
        const code = typeof result?.message === "string" ? result.message : "browserVoiceFailed";
        throw {code};
      }
      return result;
    }

    async startBrowserVoice(capability, generation) {
      this.status(strings[this.language || "en"].browserVoiceStarting);
      const isCurrent = () => generation === this.voiceGeneration && this.q("dialog").open && this.isCurrentOwnerContext();
      const session = await bootstrapAfterStartCue({
        playCue: playVoiceCue,
        createSession: () => this.realtimeRequest("session", "POST", {client: "browser_v1"}),
        isCurrent,
        stopSession: (sessionId) => this.realtimeRequest("stop", "POST", {session_id: sessionId}),
      });
      if (!session) return;
      if (session?.client !== "browser_v1" ||
          session.websocket_url !== capability.websocket_url || typeof session.session_id !== "string" ||
          typeof session.value !== "string" || !session.value || !session.setup ||
          !Number.isFinite(session.lease_ttl_seconds) || typeof session.expires_at !== "number" ||
          !Number.isSafeInteger(session.expires_at)) {
        if (typeof session?.session_id === "string") {
          void this.realtimeRequest("stop", "POST", {session_id: session.session_id}).catch(() => {});
        }
        throw {code: "browserVoiceFailed"};
      }
      const voice = new BrowserGeminiVoice({...session, auth: capability.auth}, {
        onEvent: (event) => {
          if (generation !== this.voiceGeneration || !this.isCurrentOwnerContext()) return;
          this.onBrowserVoiceEvent(voice, event);
        },
        onTurn: (text, signal) => this.realtimeRequest("turn", "POST", {session_id: session.session_id, text}, signal),
        onTranscript: (event) => this.realtimeRequest("transcript", "POST", {
          session_id: session.session_id, kind: "event", event_id: event.event_id,
          role: event.role, text: event.text,
        }),
        onStop: (sessionId) => this.realtimeRequest("stop", "POST", {session_id: sessionId}),
        playCue: playVoiceCue,
      });
      this.voice = voice;
      await voice.start();
    }

    onBrowserVoiceEvent(voice, event) {
      if (event.type === "ready") this.status(this.t.listening);
      if (event.type === "speech-start") this.status(this.t.speechStart);
      if (event.type === "speech-end") this.status(this.t.speechEnd);
      if (event.type === "error") {
        const code = typeof event.code === "string" && strings[this.language || "en"][event.code]
          ? event.code : "browserVoiceFailed";
        this.status(strings[this.language || "en"][code], true);
      }
      if (event.type === "expired") this.status(this.t.timeout, true);
      if (event.type === "ended" && this.voice === voice) {
        this.voice = undefined;
        this.voiceBusy = false;
        this.busy = false;
        if (event.stopError) this.status(strings[this.language || "en"].browserVoiceStopFailed, true);
        this.controls();
      }
    }

    finishVoice() {
      if (this.voice instanceof BrowserGeminiVoice) void this.voice.finish();
      else this.voice?.finishRecording();
    }

    onVoiceEvent(event) {
      if (event.type === "stt-vad-start") this.status(this.t.speechStart);
      if (event.type === "stt-vad-end" || event.type === "recording-ended") this.status(this.t.speechEnd);
      if (event.type === "stt-end") {
        const text = event.data?.stt_output?.text;
        this.pendingVoiceText = typeof text === "string" ? text.trim() : "";
        if (this.nativeAssistRun) this.nativeAssistEndCue = Promise.resolve().then(() => playVoiceCue("end")).catch(() => {});
      }
      if (event.type === "intent-end" && this.nativeAssistRun) {
        const output = event.data?.intent_output;
        const speech = output?.response?.speech?.plain?.speech;
        this.pendingNativeAssistResponse = typeof speech === "string" ? speech.trim() : "";
        if (typeof output?.conversation_id === "string" || output?.conversation_id === null) {
          this.nativeAssistConversation = {
            scope: this.pendingNativeAssistScope,
            conversationId: output.conversation_id,
          };
        }
        this.status(this.t.nativeAssistWorking);
      }
      if (event.type === "playback-blocked") this.status(this.t.voiceReplyFailed, true);
      if (event.type === "error") {
        this.pendingVoiceText = undefined;
        this.status(this.t[this.voiceErrorCode(event.data?.code)] || this.t.failed, true);
      }
      if (event.type === "run-end" || event.type === "error") {
        const transcript = this.pendingVoiceText;
        const nativeAssist = this.nativeAssistRun;
        const response = this.pendingNativeAssistResponse;
        this.voice?.close(false);
        this.voice = undefined;
        this.voiceBusy = false;
        this.nativeAssistRun = false;
        this.pendingVoiceText = undefined;
        this.pendingNativeAssistResponse = undefined;
        if (nativeAssist && event.type === "run-end" && transcript) {
          this.messages = appendNativeAssistTurn(this.messages, transcript, response);
          this.draw();
          this.busy = true;
          this.nativeAssistBusyGeneration = this.voiceGeneration;
          this.controls();
          if (response) {
            this.status(this.t.nativeAssistHistory);
            void this.finishNativeAssistReply(this.voiceGeneration);
          } else {
            this.nativeAssistBusyGeneration = undefined;
            this.nativeAssistPipeline = undefined;
            this.busy = false;
            this.status(this.t.nativeAssistFailed, true);
            this.controls();
          }
          return;
        }
        if (event.type === "run-end" && transcript) {
          // Keep the reload guard held until send() synchronously claims the turn.
          this.busy = false;
          this.pendingVoiceText = transcript;
          const send = this.send(transcript, {voiceReply: true});
          this.pendingVoiceText = undefined;
          this.controls();
          void send;
          return;
        }
        this.busy = false;
        this.controls();
        if (event.type === "run-end") this.status(this.t.transcriptEmpty, true);
      }
    }

    async finishNativeAssistReply(generation) {
      try {
        await this.nativeAssistEndCue;
        if (this.nativeAssistBusyGeneration !== generation || generation !== this.voiceGeneration || !this.isCurrentOwnerContext()) return;
        await this.speakReply(this.messages);
      } finally {
        if (this.nativeAssistBusyGeneration !== generation) return;
        this.nativeAssistBusyGeneration = undefined;
        if (generation !== this.voiceGeneration || !this.isCurrentOwnerContext()) return;
        this.nativeAssistPipeline = undefined;
        this.busy = false;
        this.controls();
      }
    }

    voiceErrorCode(code) {
      if (code === "microphone_denied" || code === "microphone_failed" || code === "secure_context" || code === "timeout") return code;
      if (code === "not_found") return "noAssistPipeline";
      return "failed";
    }

    async speakReply(messages) {
      if (!this.q("dialog").open || !this.isCurrentOwnerContext()) return;
      const message = Array.isArray(messages) ? [...messages].reverse().find((item) => item?.role === "assistant" && typeof item.text === "string" && item.text.trim()) : undefined;
      const pipeline = this.voiceReplyPipeline;
      this.voiceReplyPipeline = undefined;
      if (!message || !pipeline?.tts_engine) return;
      const generation = this.voiceGeneration;
      const voice = new AssistVoice(this.hass, pipeline, (event) => {
        if (generation !== this.voiceGeneration || !this.isCurrentOwnerContext()) return;
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

    renderQuickReplies() {
      const quickReplies = document.createElement("div");
      quickReplies.className = "quick-replies";
      const choices = [
        [this.t.quickXiaomi, this.language === "ru" ? "У меня есть устройства Xiaomi Home" : "I have Xiaomi Home devices", "xiaomi"],
        [this.t.quickZigbee, this.language === "ru" ? "Хочу подключить устройства Zigbee" : "I want to connect Zigbee devices"],
        [this.t.quickOther, this.language === "ru" ? "У меня есть другое оборудование" : "I have other equipment"],
      ];
      for (const [label, prompt, setupKind] of choices) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "quick-reply";
        button.textContent = label;
        button.dataset.prompt = prompt;
        if (setupKind) button.dataset.setupKind = setupKind;
        button.disabled = this.busy || !this.sessionId;
        quickReplies.append(button);
      }
      return quickReplies;
    }

    waitForMarkdownUpgrade() {
      if (this.markdownUpgradePending || customElements.get("ha-markdown")) return;
      this.markdownUpgradePending = true;
      const lifecycle = this.markdownUpgradeLifecycle;
      customElements.whenDefined("ha-markdown").then(() => {
        if (lifecycle !== this.markdownUpgradeLifecycle) return;
        this.markdownUpgradePending = false;
        if (!this.isConnected ||
            !visibleTextMessages(this.messages).some((message) => message.role === "assistant")) return;
        this.draw({preserveScroll: true});
      });
    }

    draw({preserveScroll = false} = {}) {
      const drawGeneration = ++this.drawGeneration;
      const log = this.q("#log");
      const messageContainer = this.q("#messages");
      const activeElement = this.shadowRoot.activeElement;
      const feedbackControls = activeElement?.className === "answer-feedback-comment"
        ? activeElement.closest(".answer-feedback") : undefined;
      const focusedFeedbackEditor = feedbackControls ? {
        answerIndex: feedbackControls.dataset.answerIndex,
        selectionStart: activeElement.selectionStart,
        selectionEnd: activeElement.selectionEnd,
        selectionDirection: activeElement.selectionDirection,
      } : undefined;
      const previousScrollTop = log.scrollTop;
      const wasAtBottom = log.scrollHeight - log.clientHeight - previousScrollTop <= 1;
      const keepScrollPosition = preserveScroll && !wasAtBottom;
      messageContainer.replaceChildren();
      const visibleMessages = visibleTextMessages(this.messages);
      if (!visibleMessages.length) {
        const empty = document.createElement("div"); empty.className = "empty";
        const title = document.createElement("h3"); title.textContent = this.t.intro;
        empty.append(title, this.renderQuickReplies());
        messageContainer.append(empty);
      }
      for (const message of visibleMessages) {
        const row = document.createElement("div"); row.className = `message ${message.role}`;
        row.dataset.messageIndex = String(this.messages.indexOf(message));
        const name = document.createElement("div"); name.className = "name"; name.textContent = message.role === "user" ? this.t.you : this.t.assistant;
        const markdown = message.role === "assistant" && customElements.get("ha-markdown");
        if (message.role === "assistant" && !markdown) this.waitForMarkdownUpgrade();
        const text = document.createElement(markdown ? "ha-markdown" : "p");
        if (markdown) {text.content = message.text; text.breaks = true;}
        else text.textContent = message.text;
        row.append(name);
        if (typeof message.text === "string" && message.text.trim()) row.append(text);
        if (message.role === "assistant") {
          const tools = createToolCalls(document, message.tool_calls, strings[this.language]);
          if (tools) row.append(tools);
          if (typeof message.text === "string" && message.text.trim()) row.append(this.renderAnswerFeedback(this.messages.indexOf(message)));
        }
        messageContainer.append(row);
      }
      if (focusedFeedbackEditor) {
        const textarea = messageContainer.querySelector(
          `[data-message-index="${focusedFeedbackEditor.answerIndex}"] .answer-feedback-comment`,
        );
        if (textarea) {
          textarea.focus({preventScroll: true});
          if (Number.isInteger(focusedFeedbackEditor.selectionStart) &&
              Number.isInteger(focusedFeedbackEditor.selectionEnd)) {
            textarea.setSelectionRange(
              focusedFeedbackEditor.selectionStart,
              focusedFeedbackEditor.selectionEnd,
              focusedFeedbackEditor.selectionDirection || "none",
            );
          }
        }
      }
      log.scrollTop = visibleMessages.length
        ? keepScrollPosition ? previousScrollTop : log.scrollHeight
        : 0;
      const tail = log.lastChild;
      Promise.allSettled([...messageContainer.querySelectorAll("ha-markdown")].map(item => item.updateComplete)).then(() => {
        if (!this.isConnected || this.drawGeneration !== drawGeneration || log.lastChild !== tail) return;
        const dismissalKey = !this.demo && this.isCurrentOwnerContext()
          ? chatDismissalStorageKey(this.ownerId, this.sessionId) : undefined;
        if (dismissalKey) {
          let storage;
          try { storage = globalThis.localStorage; } catch (_) { storage = undefined; }
          const dismissed = readChatDismissals(storage, dismissalKey);
          visibleMessages.forEach((message) => {
            if (message.role !== "assistant") return;
            const messageIndex = this.messages.indexOf(message);
            const row = messageContainer.querySelector(`[data-message-index="${messageIndex}"]`);
            const markdown = row?.querySelector("ha-markdown");
            if (!markdown) return;
            decorateChatDetails(markdown, {
              messageIndex,
              messageText: message.text,
              dismissalKey,
              dismissed,
              storage,
              hideLabel: this.t.hideChatBlock,
            });
          });
        }
        if (visibleMessages.length) log.scrollTop = keepScrollPosition ? previousScrollTop : log.scrollHeight;
      });
    }

    renderAnswerFeedback(messageIndex) {
      const context = assistantFeedbackContext(this.messages, messageIndex);
      const key = this.answerFeedbackKey(messageIndex);
      const state = this.feedbackStates.get(key);
      const controls = document.createElement("div");
      controls.className = "answer-feedback";
      controls.dataset.answerIndex = String(messageIndex);
      for (const [rating, label] of [
        ["positive", this.t.feedbackPositiveLabel],
        ["negative", this.t.feedbackNegativeLabel],
      ]) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = rating === "positive" ? "👍" : "👎";
        button.dataset.rating = rating;
        button.title = label;
        button.setAttribute("aria-label", label);
        button.setAttribute("aria-pressed", String(
          state?.rating === rating && ["editing", "error", "success"].includes(state?.status)
        ));
        button.disabled = !context || state?.status === "pending" || state?.status === "success";
        button.onclick = () => this.openAnswerFeedback(messageIndex, rating);
        controls.append(button);
      }
      if (state && ["editing", "error"].includes(state.status)) {
        const form = document.createElement("form");
        form.className = "answer-feedback-editor";
        form.setAttribute("aria-label", this.t.feedbackCommentLabel);
        const label = document.createElement("label");
        const textarea = document.createElement("textarea");
        textarea.className = "answer-feedback-comment";
        textarea.id = `answer-feedback-comment-${messageIndex}`;
        textarea.maxLength = MAX_ANSWER_FEEDBACK_COMMENT_CHARS;
        textarea.rows = 3;
        textarea.value = state.comment || "";
        textarea.placeholder = this.t.feedbackCommentPlaceholder;
        textarea.setAttribute("aria-label", this.t.feedbackCommentLabel);
        label.htmlFor = textarea.id;
        label.textContent = this.t.feedbackCommentLabel;
        textarea.oninput = () => {
          const current = this.feedbackStates.get(key);
          if (!current || current.status === "pending") return;
          current.comment = textarea.value.slice(0, MAX_ANSWER_FEEDBACK_COMMENT_CHARS);
          if (current.submittedComment !== current.comment) current.idempotencyKey = undefined;
        };
        const actions = document.createElement("div");
        actions.className = "answer-feedback-actions";
        const submit = document.createElement("button");
        submit.type = "submit";
        submit.className = "answer-feedback-send";
        submit.textContent = this.t.feedbackSubmit;
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.textContent = this.t.feedbackCancel;
        cancel.onclick = () => this.cancelAnswerFeedback(messageIndex);
        actions.append(submit, cancel);
        form.onsubmit = (event) => {
          event.preventDefault();
          return this.submitAnswerFeedback(messageIndex, state.rating, textarea.value);
        };
        form.append(label, textarea, actions);
        controls.append(form);
      }
      if (state?.status === "pending") {
        const status = document.createElement("span");
        status.setAttribute("role", "status");
        status.textContent = this.t.feedbackSending;
        controls.append(status);
      } else if (state?.status === "success" || state?.status === "error") {
        const status = document.createElement("span");
        status.setAttribute("role", "status");
        status.textContent = state.text;
        controls.append(status);
      }
      return controls;
    }

    openAnswerFeedback(messageIndex, rating) {
      if (!this.isCurrentOwnerContext()) return;
      const context = assistantFeedbackContext(this.messages, messageIndex);
      if (!context) return;
      const key = this.answerFeedbackKey(messageIndex);
      const previous = this.feedbackStates.get(key);
      if (previous?.status === "pending" || previous?.status === "success") return;
      this.feedbackStates.set(key, {
        ...previous,
        status: "editing",
        rating,
        comment: previous?.comment || "",
        idempotencyKey: previous?.rating === rating ? previous.idempotencyKey : undefined,
      });
      this.draw({preserveScroll: true});
      this.shadowRoot.querySelector(
        `.answer-feedback[data-answer-index="${messageIndex}"] .answer-feedback-comment`,
      )?.focus({preventScroll: true});
    }

    cancelAnswerFeedback(messageIndex) {
      const key = this.answerFeedbackKey(messageIndex);
      const previous = this.feedbackStates.get(key);
      if (!previous || previous.status === "pending" || previous.status === "success") return;
      this.feedbackStates.set(key, {...previous, status: "draft"});
      this.draw({preserveScroll: true});
      this.shadowRoot.querySelector(
        `.answer-feedback[data-answer-index="${messageIndex}"] > button[data-rating="${previous.rating}"]`,
      )?.focus({preventScroll: true});
    }

    answerFeedbackKey(messageIndex) {
      const answerText = this.messages?.[messageIndex]?.text;
      return `${this.sessionId}:${messageIndex}:${typeof answerText === "string" ? answerText : ""}`;
    }

    async submitAnswerFeedback(messageIndex, rating, commentValue) {
      if (!this.isCurrentOwnerContext() || !this.sessionId) return;
      const context = assistantFeedbackContext(this.messages, messageIndex);
      if (!context) return;
      const key = this.answerFeedbackKey(messageIndex);
      const previous = this.feedbackStates.get(key);
      if (previous?.status === "pending" || previous?.status === "success") return;
      const comment = typeof commentValue === "string"
        ? commentValue.slice(0, MAX_ANSWER_FEEDBACK_COMMENT_CHARS) : "";
      const feedback = comment
        ? `${rating === "positive" ? this.t.feedbackPositive : this.t.feedbackNegative}\n\n${comment}`
        : (rating === "positive" ? this.t.feedbackPositive : this.t.feedbackNegative);
      const sameAttempt = previous?.rating === rating &&
        previous?.submittedComment === comment && previous?.idempotencyKey;
      const attempt = sameAttempt ? previous.idempotencyKey : createFeedbackIdempotencyKey();
      this.feedbackStates.set(key, {
        ...previous,
        status: "pending", rating, comment, submittedComment: comment,
        submittedFeedback: feedback, idempotencyKey: attempt,
      });
      this.draw({preserveScroll: true});
      try {
        const response = await this.hass.fetchWithAuth("/api/habab_home/feedback", {
          method: "POST", cache: "no-store",
          headers: {"Content-Type": "application/json", Accept: "application/json"},
          body: JSON.stringify({
            session_id: this.sessionId, feedback, consent: true,
            idempotency_key: attempt, ...context,
          }),
        });
        let receipt;
        try { receipt = await response.json(); } catch (_) { receipt = undefined; }
        if (!response.ok || receipt?.state !== "saved_locally") {
          throw new Error(receipt?.message || "feedback_failed");
        }
        if (receipt.delivery_status !== "saved_on_server") {
          throw new Error(receipt.delivery_status === "not_configured" || receipt.delivery_status === "rejected"
            ? "feedback_not_configured" : "feedback_pending");
        }
        this.feedbackStates.set(key, {
          ...this.feedbackStates.get(key), status: "success", rating, idempotencyKey: attempt,
          text: receipt.trace_status === "unavailable" ? this.t.feedbackSentWithoutTrace
            : receipt.trace_status === "truncated" ? this.t.feedbackSentTruncated : this.t.feedbackSent,
        });
      } catch (error) {
        const code = error?.message;
        const text = code === "feedback_not_configured" ? this.t.feedbackNotConfigured
          : code === "feedback_pending" ? this.t.feedbackPending : this.t.feedbackFailed;
        this.feedbackStates.set(key, {
          ...this.feedbackStates.get(key), status: "error", rating, idempotencyKey: attempt, text,
        });
      }
      this.draw({preserveScroll: true});
    }

    async send(textValue, {voiceReply = false, clearDraft = false} = {}) {
      if (!this.isCurrentOwnerContext()) return;
      const text = (typeof textValue === "string" ? textValue : this.q(".composer-textarea").value).trim();
      if (!text || !this.sessionId || this.busy || this.setupRestarting) return;
      this.followSetupForms = true;
      this.busy = true; this.running = true; this.stopped = false;
      this.messages.push({role: "user", text});
      if (typeof textValue !== "string" || clearDraft) this.q(".composer-textarea").value = "";
      this.draw(); this.status(this.t.working); this.controls();
      try {
        const result = this.demo ? await this.demoRequest("", "POST", text)
          : await this.hass.callWS({type: "habab_home/agent/run", session_id: this.sessionId, text});
        this.messages = result.messages; this.draw(); this.status(this.stopped ? this.t.stopped : "");
        if (voiceReply) await this.speakReply(result.messages);
      } catch (error) { this.status(this.stopped ? this.t.stopped : await this.errorMessage(error), true); }
      finally {
        if (!this.demo) await this.loadSetup(true);
        this.busy = false; this.running = false; this.controls();
        if (this.q("dialog").open && this.focusComposerOnOpen && !matchMedia("(max-width: 720px)").matches) this.q(".composer-textarea").focus({preventScroll: true});
        this.startPendingAssist();
      }
    }

    async stop() {
      if (!this.isCurrentOwnerContext()) return;
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
  let currentOwnerId;
  let assistantContextState = "unknown";
  const privateContextIsCurrent = (element) => Boolean(element && !element.demo &&
    assistantContextState === "admin" && element.contextAvailable === true &&
    !element.hidden && !element.inert && element.ownerId === currentOwnerId &&
    element.isCurrentOwnerContext());
  const assistantHook = {
    get available() {
      return launcher?.parentNode === document.body && privateContextIsCurrent(launcher);
    },
    open(params, opener) {
      if (!this.available || !launcher) return false;
      return launcher.open(params, opener);
    },
  };
  window.addEventListener("show-dialog", (event) => {
    const detail = event.detail;
    if (detail?.dialogTag !== "ha-voice-command-dialog" || !launcher) return;
    if (!launcher.demo && !privateContextIsCurrent(launcher)) return;
    if (launcher.hidden || launcher.inert) return;
    const opener = event.composedPath?.().find((element) => element instanceof HTMLElement);
    if (launcher.open(detail.dialogParams, opener) !== true) return;
    event.stopPropagation();
    event.preventDefault();
  }, true);
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
    const autoOpenOnConnect = true;
    const hass = main?.hass;
    if (launcher && launcher.demo !== demo) {
      launcher.remove();
      launcher = undefined;
      currentOwnerId = undefined;
    }
    if (demo) {
      assistantContextState = "demo";
      currentOwnerId = undefined;
    } else {
      const context = classifyAssistantContext(currentOwnerId, main);
      if (context.state === "unknown") {
        assistantContextState = "unknown";
        launcher?.setContextAvailable(false);
        syncAssistantHook();
        return;
      }
      if (context.state === "non-admin") {
        assistantContextState = "non-admin";
        launcher?.remove();
        launcher = undefined;
        currentOwnerId = undefined;
        syncAssistantHook();
        return;
      }
      if (context.state === "different-user") {
        launcher?.remove();
        launcher = undefined;
      }
      assistantContextState = "admin";
      currentOwnerId = context.ownerId;
    }
    if (!launcher) {
      launcher = document.createElement("habab-setup-chat");
    }
    launcher.demo = demo;
    launcher.autoOpenOnConnect = autoOpenOnConnect;
    if (!demo) {
      launcher.ownerId = currentOwnerId;
      launcher.hass = hass;
      launcher.setContextAvailable(true);
    } else {
      launcher.hass = hass;
    }
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
