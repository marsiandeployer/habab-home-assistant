const CHANNELS = new Set(["all", "web", "telegram", "voice"]);
const STATUSES = new Set(["all", "active", "error", "completed", "cancelled", "unknown"]);
const SENSITIVE_KEY = /(?:api.?key|(?:^|[_-])key$|secret|password|token|cookie|authorization|credential)/i;
const CREDENTIAL_PATTERNS = [
  /\bBearer\s+[A-Za-z0-9._~+/-]+=*/gi,
  /\bsk-[A-Za-z0-9_-]{16,}\b/g,
  /\bAIza[A-Za-z0-9_-]{20,}\b/g,
  /\b\d{6,}:[A-Za-z0-9_-]{20,}\b/g,
  /([?&](?:oauth[_-]?token|id[_-]?token|access[_-]?token|refresh[_-]?token|client[_-]?secret|api[_-]?key|authorization|credential)=)[^&#\s]*/gi,
  /(^|[\s"'({,;])(?:[A-Z0-9]+[_-]+)*(?:API[_ -]?KEY|PRIVATE[_ -]?KEY|CLIENT[_ -]?SECRET|ACCESS[_ -]?TOKEN|REFRESH[_ -]?TOKEN|SESSION[_ -]?TOKEN|SIGNING[_ -]?KEY|ENCRYPTION[_ -]?KEY|SECRET|TOKEN|PASSWORD|COOKIE|AUTHORIZATION|CREDENTIAL|KEY)\b\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^&#\s,;]+)/gi,
];

const copy = {
  en: {
    title: "History and diagnostics",
    intro: "Review recorded conversations and download a diagnostic archive to your device. Nothing is sent to support automatically.",
    evidenceNote: "A model response is not proof that an action ran. A recorded tool result is not an independent Home Assistant state readback.",
    filters: "Filters",
    from: "From",
    to: "To",
    channel: "Channel",
    allChannels: "All channels",
    web: "Web chat",
    telegram: "Telegram",
    voice: "Browser voice",
    nativeAssist: "Native Assist history is not recorded by Habab.",
    status: "Result",
    allStatuses: "All results",
    active: "Active",
    error: "Error",
    completed: "Completed",
    cancelled: "Cancelled",
    unknown: "Unknown",
    sessionFilter: "Session ID",
    apply: "Apply filters",
    refresh: "Refresh",
    loading: "Loading conversations…",
    loadingDetail: "Loading conversation…",
    empty: "No recorded conversations match these filters.",
    noDetail: "Select a conversation to review its recorded events.",
    unavailable: "Unavailable",
    notRecorded: "This stage was not recorded.",
    sourceUnavailable: "This source is unavailable.",
    messages: "messages",
    conversation: "Conversation",
    events: "Recorded events",
    date: "Time",
    event: "Event",
    details: "Details",
    userMessage: "User message",
    assistantText: "Assistant text",
    voiceTranscript: "Voice transcript",
    toolInvocation: "Tool invocation recorded",
    toolOutput: "Tool result recorded",
    stageError: "Runtime error recorded",
    unavailableStage: "Stage unavailable",
    toolCallRecorded: "Tool call recorded by runtime",
    toolResultRecorded: "Tool result recorded by runtime",
    runtimeErrorRecorded: "Error recorded by runtime",
    notAction: "Not an action",
    unconfirmed: "Not confirmed",
    confirmationUnknown: "Confirmation unknown",
    toolStatus: "Tool status",
    exitCode: "Exit code",
    tool: "Tool",
    input: "Arguments",
    output: "Result",
    failureDetails: "Failure details",
    loadMore: "Load more",
    sources: "Diagnostic sources",
    available: "Available",
    selectedDownload: "Export selected conversation",
    periodDownload: "Export period and channel",
    periodHelp: "Choose both start and end times. Status and session ID filters do not narrow the exported archive.",
    downloading: "Preparing the archive…",
    downloadStarted: "Download started on this device.",
    sessionMissing: "This conversation is no longer available. Refresh the list.",
    invalidFilters: "Check the time range and selected filters.",
    accessDenied: "This page is available to Home Assistant administrators only.",
    loadError: "Could not load diagnostics. Check your connection and try again.",
    exportError: "Could not prepare the diagnostic archive. Try again.",
    invalidResponse: "The diagnostics response could not be verified.",
    unauthorized: "Your Home Assistant session is no longer authorized. Sign in again.",
    expired: "This conversation is no longer available.",
    truncated: "The current runtime may limit the conversation history available here.",
    atMessageLimit: "This conversation reached the current message limit; older messages may be omitted.",
    redacted: "Sensitive fields are masked.",
    noAudio: "Audio is not included.",
    stageLimits: "Backend and model acknowledgements, voice transport details, and some cancellation outcomes are not recorded. Cancellation is shown only when the runtime records a recognized abort; missing events do not prove an action did not occur.",
    reasonNotRecorded: "Not recorded",
    reasonNotCollected: "Not collected",
    reasonNotExposed: "Not exposed by the current runtime",
    reasonNotRecordedByHabab: "Not recorded by Habab",
    summary: "Conversation summary",
    created: "Started",
    updated: "Updated",
    channelLabel: "Channel",
    statusLabel: "Status",
    idLabel: "Session",
    countLabel: "Message count",
    durationLabel: "Processing time",
    durationUnavailable: "Not recorded",
    providerLabel: "Provider / model",
    processingTimeNote: "Processing time is summed from recorded assistant message intervals; it excludes user round-trip time.",
    eventDuration: "Processing time",
    sourceHistory: "Habab conversation history",
    sourceBackendAck: "Backend acknowledgement",
    sourceModelRequest: "Model request",
    sourceOpenCodeFiles: "OpenCode files",
    sourceHaLogs: "Home Assistant logs",
    sourceVoice: "Voice transport",
    sourceNativeAssist: "Native Assist history",
    running: "Running",
  },
  ru: {
    title: "История и диагностика",
    intro: "Просматривайте сохранённые разговоры и скачивайте диагностический архив на своё устройство. Данные не отправляются в поддержку автоматически.",
    evidenceNote: "Ответ модели не доказывает, что действие выполнено. Запись результата инструмента не является отдельной проверкой состояния Home Assistant.",
    filters: "Фильтры",
    from: "Начало",
    to: "Конец",
    channel: "Канал",
    allChannels: "Все каналы",
    web: "Веб-чат",
    telegram: "Telegram",
    voice: "Голос в браузере",
    nativeAssist: "История нативного Assist не записывается Habab.",
    status: "Результат",
    allStatuses: "Все результаты",
    active: "Активен",
    error: "Ошибка",
    completed: "Завершён",
    cancelled: "Отменён",
    unknown: "Неизвестно",
    sessionFilter: "ID сессии",
    apply: "Применить фильтры",
    refresh: "Обновить",
    loading: "Загружаем разговоры…",
    loadingDetail: "Загружаем разговор…",
    empty: "Нет записанных разговоров с такими фильтрами.",
    noDetail: "Выберите разговор, чтобы просмотреть записанные события.",
    unavailable: "Недоступно",
    notRecorded: "Этот этап не записывался.",
    sourceUnavailable: "Этот источник недоступен.",
    messages: "сообщений",
    conversation: "Разговор",
    events: "Записанные события",
    date: "Время",
    event: "Событие",
    details: "Подробности",
    userMessage: "Сообщение пользователя",
    assistantText: "Текст помощника",
    voiceTranscript: "Расшифровка речи",
    toolInvocation: "Вызов инструмента записан",
    toolOutput: "Результат инструмента записан",
    stageError: "Ошибка runtime записана",
    unavailableStage: "Этап недоступен",
    toolCallRecorded: "Вызов записан runtime",
    toolResultRecorded: "Результат записан runtime",
    runtimeErrorRecorded: "Ошибка записана runtime",
    notAction: "Не является действием",
    unconfirmed: "Не подтверждено",
    confirmationUnknown: "Подтверждение неизвестно",
    toolStatus: "Статус инструмента",
    exitCode: "Код завершения",
    tool: "Инструмент",
    input: "Аргументы",
    output: "Результат",
    failureDetails: "Сведения об ошибке",
    loadMore: "Загрузить ещё",
    sources: "Источники диагностики",
    available: "Доступен",
    selectedDownload: "Экспортировать выбранный разговор",
    periodDownload: "Экспортировать период и канал",
    periodHelp: "Укажите начало и конец. Фильтры результата и ID сессии не ограничивают архив.",
    downloading: "Готовим архив…",
    downloadStarted: "Скачивание архива началось на этом устройстве.",
    sessionMissing: "Разговор больше недоступен. Обновите список.",
    invalidFilters: "Проверьте период и выбранные фильтры.",
    accessDenied: "Страница доступна только администраторам Home Assistant.",
    loadError: "Не удалось загрузить диагностику. Проверьте подключение и повторите попытку.",
    exportError: "Не удалось подготовить диагностический архив. Повторите попытку.",
    invalidResponse: "Не удалось проверить ответ диагностики.",
    unauthorized: "Сеанс Home Assistant больше не авторизован. Войдите снова.",
    expired: "Разговор больше недоступен.",
    truncated: "Текущий runtime может ограничивать доступную здесь историю разговора.",
    atMessageLimit: "Разговор достиг текущего лимита сообщений; более ранние записи могут отсутствовать.",
    redacted: "Конфиденциальные поля скрыты.",
    noAudio: "Аудиозаписи не включаются.",
    stageLimits: "Подтверждения backend и модели, детали голосового транспорта и часть сведений об отмене не записываются. Отмена отображается только при распознанной записи runtime; отсутствие события не доказывает, что действие не выполнялось.",
    reasonNotRecorded: "Не записывается",
    reasonNotCollected: "Не собирается",
    reasonNotExposed: "Не предоставляется текущим runtime",
    reasonNotRecordedByHabab: "Не записывается Habab",
    summary: "Сведения о разговоре",
    created: "Начат",
    updated: "Обновлён",
    channelLabel: "Канал",
    statusLabel: "Статус",
    idLabel: "Сессия",
    countLabel: "Количество сообщений",
    durationLabel: "Время обработки",
    durationUnavailable: "Не записана",
    providerLabel: "Провайдер / модель",
    processingTimeNote: "Время обработки суммируется по записанным интервалам сообщений помощника и не включает ожидание ответа пользователя.",
    eventDuration: "Время обработки",
    sourceHistory: "История Habab",
    sourceBackendAck: "Подтверждение backend",
    sourceModelRequest: "Запрос модели",
    sourceOpenCodeFiles: "Файлы OpenCode",
    sourceHaLogs: "Журналы Home Assistant",
    sourceVoice: "Голосовой транспорт",
    sourceNativeAssist: "История нативного Assist",
    running: "Выполняется",
  },
};

function isSensitiveKey(key) {
  return SENSITIVE_KEY.test(String(key || "")) || /^(?:key|auth)$/i.test(String(key || ""));
}

export function sanitizeDiagnosticText(value) {
  if (typeof value !== "string") return "";
  let text = value.slice(0, 8000);
  for (const pattern of CREDENTIAL_PATTERNS) text = text.replace(pattern, (match, queryPrefix) => {
    if (/^Bearer\s/i.test(match)) return "Bearer [redacted]";
    if (typeof queryPrefix === "string") return `${queryPrefix}[redacted]`;
    return "[redacted]";
  });
  text = text.replace(/(^|[\s("'=])\/(?:[^\s"'<>]+\/)*[^\s"'<>]+/g, "$1[path]");
  text = text.replace(/(^|[\s("'=])[A-Za-z]:\\(?:[^\s"'<>]+\\)*[^\s"'<>]+/g, "$1[path]");
  return text;
}

export function safeDiagnosticValue(value, key = "", depth = 0) {
  if (isSensitiveKey(key)) return "[redacted]";
  if (typeof value === "string") return sanitizeDiagnosticText(value);
  if (value === null || typeof value === "number" || typeof value === "boolean") return value;
  if (depth >= 4) return "[truncated]";
  if (Array.isArray(value)) return value.slice(0, 40).map((entry) => safeDiagnosticValue(entry, "", depth + 1));
  if (typeof value !== "object") return "[unavailable]";
  return Object.fromEntries(Object.entries(value).slice(0, 80).map(([childKey, childValue]) =>
    [childKey, safeDiagnosticValue(childValue, childKey, depth + 1)]));
}

export function normalizeDiagnosticsFilters(filters) {
  const from = filters?.from ? new Date(filters.from) : undefined;
  const to = filters?.to ? new Date(filters.to) : undefined;
  if ((from && Number.isNaN(from.getTime())) || (to && Number.isNaN(to.getTime()))) throw new Error("invalid_time");
  if (from && to && from > to) throw new Error("invalid_time_range");
  const channel = CHANNELS.has(filters?.channel) ? filters.channel : "all";
  const status = STATUSES.has(filters?.status) ? filters.status : "all";
  const sessionId = typeof filters?.session_id === "string" ? filters.session_id.trim() : "";
  if (sessionId && !/^[A-Za-z0-9_-]{1,128}$/.test(sessionId)) throw new Error("invalid_session_id");
  return {
    from: from?.toISOString(),
    to: to?.toISOString(),
    channel,
    status,
    session_id: sessionId || undefined,
  };
}

export function diagnosticQueryString(filters, cursor) {
  const normalized = normalizeDiagnosticsFilters(filters);
  const query = new URLSearchParams();
  for (const key of ["from", "to", "channel", "status", "session_id"]) {
    const value = normalized[key];
    if (value && value !== "all") query.set(key, value);
  }
  if (cursor) query.set("cursor", String(cursor));
  query.set("limit", "20");
  return query.toString();
}

export function formatDiagnosticTimestamp(value, language = "en") {
  if (typeof value !== "string" && typeof value !== "number") return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(language === "ru" ? "ru-RU" : "en-US", {
    dateStyle: "medium", timeStyle: "short",
  }).format(date);
}

export function formatDiagnosticDuration(milliseconds, language = "en") {
  if (typeof milliseconds !== "number" || !Number.isFinite(milliseconds) || milliseconds < 0) return undefined;
  const ru = language === "ru";
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ${ru ? "мс" : "ms"}`;
  const seconds = milliseconds / 1000;
  if (seconds < 60) return `${new Intl.NumberFormat(ru ? "ru-RU" : "en-US", {maximumFractionDigits: 1}).format(seconds)} ${ru ? "с" : "s"}`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = Math.round(seconds % 60);
  return ru
    ? `${minutes} мин ${remainingSeconds} с`
    : `${minutes} min ${remainingSeconds} s`;
}

export function formatDiagnosticProviderModels(providerModels, language = "en") {
  if (!Array.isArray(providerModels)) return "";
  const pairs = providerModels
    .filter((entry) => entry && typeof entry.provider === "string" && typeof entry.model === "string")
    .map((entry) => `${sanitizeDiagnosticText(entry.provider)} — ${sanitizeDiagnosticText(entry.model)}`);
  return [...new Set(pairs)].join(language === "ru" ? "; " : "; ");
}

export function diagnosticEventLabel(event, words) {
  if (!event || event.available === false || event.kind === "stage_unavailable") return words.unavailableStage;
  const labels = {
    user_message: words.userMessage,
    assistant_text: words.assistantText,
    voice_transcript: words.voiceTranscript,
    tool_invocation: words.toolInvocation,
    tool_output: words.toolOutput,
    stage_error: words.stageError,
  };
  return labels[event.kind] || words.unknown;
}

function el(name, className, text) {
  const node = document.createElement(name);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function diagnosticSessionKey(session) {
  return `${session.channel}:${session.session_id}`;
}

class HababDiagnosticsPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this.filters = {from: "", to: "", channel: "all", status: "all", session_id: ""};
    this.sessions = [];
    this.sources = undefined;
    this.selectedSession = undefined;
    this.detail = undefined;
    this.cursor = undefined;
    this.loading = false;
    this.loadingDetail = false;
    this.exporting = false;
    this.requestVersion = 0;
    this.detailVersion = 0;
    this.loadedOwner = undefined;
    this.exportTimer = undefined;
  }

  set hass(value) {
    const previousOwner = this._hass?.user?.id;
    this._hass = value;
    const ownerId = value?.user?.id;
    if (previousOwner !== undefined && ownerId !== previousOwner) {
      this.abortRequests();
      this.clearPrivateData();
      this.loadedOwner = undefined;
    }
    const language = value?.locale?.language?.startsWith("ru") ? "ru" : "en";
    if (!this.shadowRoot.firstChild || language !== this.language) {
      this.language = language;
      this.render();
    }
    if (value?.user?.is_admin !== true) {
      this.abortRequests();
      this.clearPrivateData();
      this.loadedOwner = undefined;
      this.renderStatus(this.t("accessDenied"), true);
      return;
    }
    if (ownerId !== this.loadedOwner) {
      this.loadedOwner = ownerId;
      if (this.isConnected) void this.loadSessions(false);
    }
    const menu = this.q("ha-menu-button");
    if (menu) menu.hass = value;
  }

  connectedCallback() {
    if (!this.shadowRoot.firstChild) this.render();
    if (this._hass?.user?.is_admin === true && (
      this._hass.user.id !== this.loadedOwner || this.needsReload || (this.sources === undefined && !this.loading)
    )) {
      this.loadedOwner = this._hass.user.id;
      this.needsReload = false;
      void this.loadSessions(false);
    }
  }

  disconnectedCallback() {
    this.needsReload = this.loading || this.loadingDetail;
    this.abortRequests();
    this.loading = false;
    this.loadingDetail = false;
    clearTimeout(this.exportTimer);
    if (this.exportObjectUrl) URL.revokeObjectURL(this.exportObjectUrl);
    this.exportObjectUrl = undefined;
  }

  get words() { return copy[this.language || "en"]; }
  t(key) { return this.words[key] || this.words.unknown; }
  q(selector) { return this.shadowRoot.querySelector(selector); }

  render() {
    const t = (key) => this.t(key);
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;height:100%;overflow:auto;color:var(--primary-text-color);background:var(--primary-background-color);font:14px/1.5 var(--paper-font-body1_-_font-family,Roboto,system-ui,sans-serif);scrollbar-color:var(--secondary-text-color) transparent;scrollbar-width:thin}
      *{box-sizing:border-box}header{display:flex;align-items:center;gap:var(--ha-space-4,16px);min-height:56px;padding:8px 16px;border-block-end:1px solid var(--divider-color);background:var(--card-background-color)}
      h1{font-size:20px;font-weight:500;margin:0}main{max-width:1200px;margin:auto;padding:var(--ha-space-6,24px)}p{max-width:80ch;margin:0 0 var(--ha-space-3,12px)}
      .intro,.help,.muted{color:var(--secondary-text-color)}.notice{padding:12px;border-inline-start:4px solid var(--primary-color);background:var(--secondary-background-color);margin-block:12px 20px}
      form{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));align-items:end;gap:var(--ha-space-3,12px);padding:var(--ha-space-4,16px);background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:10px;margin-block:16px}
      label{display:grid;gap:5px;font-weight:500}input,select,button{font:inherit;color:var(--primary-text-color)}input,select{width:100%;min-height:44px;padding:8px 10px;border:1px solid var(--secondary-text-color);border-radius:6px;background:var(--card-background-color)}
      button{cursor:pointer;min-height:44px;padding:8px 12px;border:1px solid var(--divider-color);border-radius:8px;background:var(--card-background-color)}button:hover:not(:disabled){background:var(--secondary-background-color)}button:disabled{cursor:default;opacity:.55}
      button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid var(--ha-color-focus,var(--primary-color));outline-offset:2px}.primary{background:var(--ha-color-fill-primary-loud-hover,var(--dark-primary-color));border-color:transparent;color:var(--ha-color-on-primary-loud,var(--text-primary-color))}
      .actions{display:flex;flex-wrap:wrap;gap:8px;margin-block:12px}.filters-actions{display:flex;gap:8px;grid-column:span 2}.filters-actions button{flex:1}
      .status{min-height:24px;padding-block:4px}.status[data-error=true]{color:var(--error-color)}.workspace{display:grid;grid-template-columns:minmax(280px,0.8fr) minmax(0,1.6fr);gap:var(--ha-space-4,16px);align-items:start}
      .panel{min-width:0;background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:10px;overflow:hidden}.panel h2{font-size:18px;font-weight:500;margin:0;padding:14px 16px;border-block-end:1px solid var(--divider-color)}
      .session-list{list-style:none;padding:0;margin:0}.session-list li+li{border-block-start:1px solid var(--divider-color)}.session{display:grid;width:100%;min-height:78px;text-align:start;border:0;border-radius:0;padding:12px 16px;gap:3px}.session[aria-current=true]{background:var(--secondary-background-color);box-shadow:inset 3px 0 var(--primary-color)}.session-title{font-weight:500;overflow-wrap:anywhere}.session-meta{color:var(--secondary-text-color);font-size:12px;display:flex;flex-wrap:wrap;gap:4px 10px}
      .panel-body{padding:14px 16px}.empty{padding:18px 16px;color:var(--secondary-text-color)}.summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:0 0 14px}.summary div{min-width:0}.summary dt{color:var(--secondary-text-color);font-size:12px}.summary dd{margin:2px 0 0;overflow-wrap:anywhere}
      .event-list{list-style:none;margin:0;padding:0;border-inline-start:2px solid var(--divider-color)}.event{position:relative;padding:4px 0 18px 18px}.event:before{content:"";position:absolute;inset-block-start:10px;inset-inline-start:-6px;width:10px;height:10px;border-radius:50%;background:var(--primary-color);border:1px solid var(--card-background-color)}.event h3{font-size:15px;font-weight:500;margin:0 0 2px}.event time{font-size:12px;color:var(--secondary-text-color)}.event-text{white-space:pre-wrap;overflow-wrap:anywhere;margin-block:8px}.event details{margin-block:8px}.event summary{cursor:pointer;min-height:40px;display:flex;align-items:center;text-decoration:underline;text-underline-offset:3px}.code{font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--secondary-background-color);padding:10px;border-radius:6px;margin:0;max-height:360px;overflow:auto}
    .sources{margin-block:16px;border-block-start:1px solid var(--divider-color);padding-block-start:12px}.sources h3{font-size:15px;font-weight:500;margin:0 0 8px}.source-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px 16px;margin:0;padding:0;list-style:none}.source-list li{display:flex;justify-content:space-between;gap:10px;padding-block:4px;border-block-end:1px solid var(--divider-color)}.source-state{flex:none;color:var(--secondary-text-color)}.source-name{display:grid;gap:2px}.source-reason{font-size:12px;color:var(--secondary-text-color)}.warning{color:var(--warning-color,var(--primary-text-color))}
      [hidden]{display:none!important}a{color:var(--primary-color)}
      @media(max-width:900px){main{padding:16px}form{grid-template-columns:repeat(2,minmax(0,1fr))}.filters-actions{grid-column:span 2}.workspace{grid-template-columns:minmax(0,1fr)}.source-list{grid-template-columns:minmax(0,1fr)}}
      @media(max-width:520px){main{padding:12px 12px 24px}header{min-height:52px}form{grid-template-columns:minmax(0,1fr);padding:12px}.filters-actions{grid-column:auto;display:grid;grid-template-columns:1fr}.summary{grid-template-columns:minmax(0,1fr)}.panel h2{padding:12px}.panel-body{padding:12px}.session{padding:12px}.notice{margin-block:10px 14px}}
    </style>
    <header><ha-menu-button></ha-menu-button><h1>${t("title")}</h1></header>
    <main>
      <p class="intro">${t("intro")}</p>
      <p class="notice" id="evidence-note">${t("evidenceNote")}</p>
      <p class="help" id="native-assist-note">${t("nativeAssist")}</p>
      <form id="filters" aria-label="${t("filters")}">
        <label>${t("from")}<input id="from" name="from" type="datetime-local"></label>
        <label>${t("to")}<input id="to" name="to" type="datetime-local"></label>
        <label>${t("channel")}<select id="channel" name="channel"><option value="all">${t("allChannels")}</option><option value="web">${t("web")}</option><option value="telegram">${t("telegram")}</option><option value="voice">${t("voice")}</option></select></label>
        <label>${t("status")}<select id="status-filter" name="status"><option value="all">${t("allStatuses")}</option><option value="active">${t("active")}</option><option value="error">${t("error")}</option><option value="completed">${t("completed")}</option><option value="cancelled">${t("cancelled")}</option><option value="unknown">${t("unknown")}</option></select></label>
        <label>${t("sessionFilter")}<input id="session-id" name="session_id" type="text" maxlength="128" autocomplete="off"></label>
        <div class="filters-actions"><button class="primary" type="submit">${t("apply")}</button><button type="button" id="refresh">${t("refresh")}</button></div>
      </form>
      <p id="status" class="status" role="status" aria-live="polite"></p>
      <div class="actions"><button type="button" id="download-session" disabled>${t("selectedDownload")}</button><button type="button" id="download-period" disabled title="${t("periodHelp")}">${t("periodDownload")}</button></div>
      <p class="help" id="period-help">${t("periodHelp")}</p>
      <div class="workspace">
        <section class="panel" aria-labelledby="sessions-heading"><h2 id="sessions-heading">${t("conversation")}</h2><div id="session-list"></div><div class="panel-body"><button type="button" id="load-more" hidden>${t("loadMore")}</button></div></section>
        <section class="panel" aria-labelledby="detail-heading"><h2 id="detail-heading">${t("events")}</h2><div class="panel-body" id="detail"></div></section>
      </div>
      <section class="sources" aria-labelledby="sources-heading"><h3 id="sources-heading">${t("sources")}</h3><p id="source-note" class="help"></p><ul id="source-list" class="source-list"></ul></section>
    </main>`;
    this.q("ha-menu-button").hass = this._hass;
    this.q("#filters").addEventListener("submit", (event) => {
      event.preventDefault();
      this.filters = {
        from: this.q("#from").value,
        to: this.q("#to").value,
        channel: this.q("#channel").value,
        status: this.q("#status-filter").value,
        session_id: this.q("#session-id").value,
      };
      void this.loadSessions(false);
    });
    this.q("#refresh").addEventListener("click", () => void this.loadSessions(false));
    this.q("#load-more").addEventListener("click", () => void this.loadSessions(true));
    this.q("#download-session").addEventListener("click", () => void this.downloadSelectedSession());
    this.q("#download-period").addEventListener("click", () => void this.downloadPeriod());
    for (const [selector, key] of [["#from", "from"], ["#to", "to"], ["#channel", "channel"], ["#status-filter", "status"], ["#session-id", "session_id"]]) {
      const input = this.q(selector);
      input.value = this.filters[key] || "";
    }
    this.updateControls();
    this.renderSessions();
    this.renderDetail();
    this.renderSources();
  }

  abortRequests() {
    this.listAbort?.abort();
    this.listAbort = undefined;
    this.detailAbort?.abort();
    this.detailAbort = undefined;
    this.exportAbort?.abort();
    this.exportAbort = undefined;
    this.requestVersion++;
    this.detailVersion++;
  }

  clearPrivateData() {
    this.sessions = [];
    this.sources = undefined;
    this.selectedSession = undefined;
    this.detail = undefined;
    this.cursor = undefined;
    this.loading = false;
    this.loadingDetail = false;
    this.renderSessions();
    this.renderDetail();
    this.renderSources();
  }

  async fetchJson(url, options) {
    const response = await this._hass.fetchWithAuth(url, options);
    if (!response.ok) {
      const error = new Error("diagnostics_request_failed");
      error.status = response.status;
      throw error;
    }
    try { return await response.json(); }
    catch (_) { throw new Error("diagnostics_invalid_response"); }
  }

  async loadSessions(append) {
    if (this._hass?.user?.is_admin !== true) return;
    let query;
    try { query = diagnosticQueryString(this.filters, append ? this.cursor : undefined); }
    catch (_) { this.renderStatus(this.t("invalidFilters"), true); return; }
    this.listAbort?.abort();
    const controller = new AbortController();
    this.listAbort = controller;
    const version = ++this.requestVersion;
    if (!append) {
      this.selectedSession = undefined;
      this.detail = undefined;
      this.cursor = undefined;
      this.sessions = [];
    }
    this.loading = true;
    this.renderStatus(this.t("loading"));
    this.renderSessions();
    this.renderDetail();
    this.updateControls();
    try {
      const result = await this.fetchJson(`/api/habab_home/diagnostics/sessions?${query}`, {
        method: "GET", cache: "no-store", signal: controller.signal,
      });
      if (version !== this.requestVersion || controller.signal.aborted) return;
      if (!Array.isArray(result?.sessions) || !result.sources ||
          (result.next_cursor !== null && result.next_cursor !== undefined && typeof result.next_cursor !== "string")) {
        throw new Error("diagnostics_invalid_response");
      }
      const existing = new Set(this.sessions.map(diagnosticSessionKey));
      this.sessions = append
        ? [...this.sessions, ...result.sessions.filter((session) => session && typeof session.session_id === "string" && typeof session.channel === "string" && !existing.has(diagnosticSessionKey(session)))]
        : result.sessions.filter((session) => session && typeof session.session_id === "string" && typeof session.channel === "string");
      this.cursor = typeof result.next_cursor === "string" && result.next_cursor ? result.next_cursor : undefined;
      this.sources = result.sources;
      this.renderStatus("");
      this.renderSessions();
      this.renderSources();
      this.updateControls();
    } catch (error) {
      if (controller.signal.aborted || version !== this.requestVersion) return;
      this.renderStatus(this.requestError(error), true);
      this.renderSessions();
      this.updateControls();
    } finally {
      if (version === this.requestVersion) {
        this.loading = false;
        this.listAbort = undefined;
        this.updateControls();
        this.renderSessions();
      }
    }
  }

  requestError(error) {
    if (error?.status === 401) return this.t("unauthorized");
    if (error?.status === 403) return this.t("accessDenied");
    if (error?.status === 404) return this.t("expired");
    return error?.message === "diagnostics_invalid_response" ? this.t("invalidResponse") : this.t("loadError");
  }

  renderStatus(text, isError = false) {
    const status = this.q("#status");
    if (!status) return;
    status.textContent = text;
    status.dataset.error = isError ? "true" : "false";
  }

  renderSessions() {
    const container = this.q("#session-list");
    if (!container) return;
    container.replaceChildren();
    if (!this.loading && !this.sessions.length) {
      container.append(el("p", "empty", this.t("empty")));
      return;
    }
    const list = el("ul", "session-list");
    for (const session of this.sessions) {
      const item = el("li");
      const button = el("button", "session");
      button.type = "button";
      button.setAttribute("aria-current", this.selectedSession && diagnosticSessionKey(this.selectedSession) === diagnosticSessionKey(session) ? "true" : "false");
      button.addEventListener("click", () => void this.openSession(session));
      button.append(el("span", "session-title", sanitizeDiagnosticText(session.title || session.session_id)));
      const metadata = el("span", "session-meta");
      metadata.append(
        el("span", "", this.t(session.channel === "telegram" ? "telegram" : session.modes?.includes("voice") ? "voice" : "web")),
        el("span", "", this.t(session.status || "unknown")),
        el("span", "", `${formatDiagnosticTimestamp(session.updated_at || session.created_at, this.language)} · ${Number.isFinite(session.message_count) ? session.message_count : 0} ${this.t("messages")}`),
      );
      button.append(metadata);
      item.append(button);
      list.append(item);
    }
    container.append(list);
  }

  async openSession(session) {
    this.detailAbort?.abort();
    const controller = new AbortController();
    this.detailAbort = controller;
    const version = ++this.detailVersion;
    this.selectedSession = session;
    this.detail = undefined;
    this.loadingDetail = true;
    this.renderSessions();
    this.renderDetail();
    this.updateControls();
    const channel = this.filters.channel === "voice" ? "voice" : session.channel;
    const query = new URLSearchParams({channel});
    try {
      const result = await this.fetchJson(`/api/habab_home/diagnostics/sessions/${encodeURIComponent(session.session_id)}?${query}`, {
        method: "GET", cache: "no-store", signal: controller.signal,
      });
      if (version !== this.detailVersion || controller.signal.aborted) return;
      if (!result?.session || !Array.isArray(result.events) || !result.sources) throw new Error("diagnostics_invalid_response");
      this.detail = result;
      this.sources = result.sources;
      this.renderStatus("");
      this.renderDetail();
      this.renderSources();
    } catch (error) {
      if (controller.signal.aborted || version !== this.detailVersion) return;
      this.detail = {error: this.requestError(error)};
      this.renderDetail();
    } finally {
      if (version === this.detailVersion) {
        this.loadingDetail = false;
        this.detailAbort = undefined;
        this.updateControls();
        this.renderDetail();
      }
    }
  }

  renderDetail() {
    const detail = this.q("#detail");
    if (!detail) return;
    detail.replaceChildren();
    if (this.loadingDetail) {
      detail.append(el("p", "empty", this.t("loadingDetail")));
      return;
    }
    if (this.detail?.error) {
      detail.append(el("p", "status", this.detail.error));
      return;
    }
    if (!this.detail) {
      detail.append(el("p", "empty", this.t("noDetail")));
      return;
    }
    const session = this.detail.session;
    const summary = el("dl", "summary");
    const summaryItems = [
      ["idLabel", session.session_id],
      ["channelLabel", this.t(session.modes?.includes("voice") && this.filters.channel === "voice" ? "voice" : session.channel === "telegram" ? "telegram" : "web")],
      ["statusLabel", this.t(session.status || "unknown")],
      ["countLabel", Number.isFinite(session.message_count) ? session.message_count : this.detail.events.length],
      ["created", formatDiagnosticTimestamp(session.created_at, this.language)],
      ["updated", formatDiagnosticTimestamp(session.updated_at, this.language)],
      ["providerLabel", formatDiagnosticProviderModels(session.provider_models, this.language) || this.t("durationUnavailable")],
      ["durationLabel", formatDiagnosticDuration(session.processing_duration_ms, this.language) || this.t("durationUnavailable")],
    ];
    for (const [label, value] of summaryItems) {
      const wrapper = el("div");
      wrapper.append(el("dt", "", this.t(label)), el("dd", "", sanitizeDiagnosticText(String(value ?? "—"))));
      summary.append(wrapper);
    }
    detail.append(summary);
    detail.append(el("p", "warning", this.t("evidenceNote")));
    detail.append(el("p", "help", this.t("processingTimeNote")));
    const events = Array.isArray(this.detail.events) ? this.detail.events : [];
    if (session.error) detail.append(this.renderData(this.t("failureDetails"), session.error));
    if (!events.length) {
      detail.append(el("p", "empty", this.t("empty")));
      return;
    }
    const list = el("ol", "event-list");
    for (const event of events) list.append(this.renderEvent(event));
    detail.append(list);
    detail.append(el("p", "help", this.t("redacted")));
    if (session.at_message_limit === true) detail.append(el("p", "help", this.t("atMessageLimit")));
    else detail.append(el("p", "help", this.t("truncated")));
  }

  renderEvent(event) {
    const item = el("li", "event");
    item.append(el("h3", "", diagnosticEventLabel(event, this.words)));
    const time = el("time", "", formatDiagnosticTimestamp(event?.at, this.language));
    if (typeof event?.at === "string") time.dateTime = event.at;
    item.append(time);
    if (event?.available === false || event?.kind === "stage_unavailable") {
      item.append(el("p", "muted", event?.reason === "not_recorded" ? this.t("notRecorded") : this.t("sourceUnavailable")));
      return item;
    }
    if (typeof event?.text === "string" && event.text) item.append(el("p", "event-text", sanitizeDiagnosticText(event.text)));
    if (typeof event?.provider === "string" || typeof event?.model === "string") {
      const providerModel = [event.provider, event.model].filter((value) => typeof value === "string").map(sanitizeDiagnosticText).join(" — ");
      item.append(el("p", "muted", providerModel));
    }
    const duration = formatDiagnosticDuration(event?.duration_ms, this.language);
    if (duration) item.append(el("p", "muted", `${this.t("eventDuration")}: ${duration}`));
    const confirmation = {
      not_applicable: this.t("notAction"),
      unconfirmed: this.t("unconfirmed"),
      unknown: this.t("confirmationUnknown"),
      runtime_call_recorded: this.t("toolCallRecorded"),
      runtime_result_recorded: this.t("toolResultRecorded"),
      runtime_error_recorded: this.t("runtimeErrorRecorded"),
      confirmed_by_tool_result: this.t("toolResultRecorded"),
    }[event?.confirmation];
    if (confirmation) item.append(el("p", "muted", confirmation));
    if (event?.tool && typeof event.tool === "object") item.append(this.renderData(this.t("tool"), event.tool));
    if (event?.input !== undefined) item.append(this.renderData(this.t("input"), event.input));
    if (event?.output !== undefined) item.append(this.renderData(event?.kind === "stage_error" ? this.t("failureDetails") : this.t("output"), event.output));
    if (event?.exit_code !== undefined && event.exit_code !== null) item.append(el("p", "muted", `${this.t("exitCode")}: ${sanitizeDiagnosticText(String(event.exit_code))}`));
    if (event?.status && (event.kind?.startsWith("tool_") || event.kind === "stage_error")) {
      const label = event.kind === "stage_error" ? this.t("statusLabel") : this.t("toolStatus");
      item.append(el("p", "muted", `${label}: ${this.t(event.status)}`));
    }
    return item;
  }

  renderData(label, value) {
    const details = el("details");
    details.append(el("summary", "", label));
    const pre = el("pre", "code");
    const safe = safeDiagnosticValue(value);
    pre.textContent = typeof safe === "string" ? safe : JSON.stringify(safe, null, 2);
    details.append(pre);
    return details;
  }

  renderSources() {
    const list = this.q("#source-list");
    const note = this.q("#source-note");
    if (!list || !note) return;
    list.replaceChildren();
    if (!this.sources || typeof this.sources !== "object") {
      note.textContent = this.t("sourceUnavailable");
      return;
    }
    note.textContent = `${this.t("noAudio")} ${this.t("redacted")} ${this.t("stageLimits")}`;
    const labels = {
      history: this.t("sourceHistory"), backend_ack: this.t("sourceBackendAck"),
      model_request: this.t("sourceModelRequest"), opencode_files: this.t("sourceOpenCodeFiles"),
      home_assistant_logs: this.t("sourceHaLogs"), voice_transport: this.t("sourceVoice"),
      native_assist_history: this.t("sourceNativeAssist"),
    };
    for (const [key, source] of Object.entries(this.sources)) {
      const item = el("li");
      const name = el("span", "source-name");
      name.append(el("span", "", labels[key] || sanitizeDiagnosticText(key)));
      const available = source?.available === true;
      const reasonLabels = {
        not_recorded: this.t("reasonNotRecorded"),
        not_collected: this.t("reasonNotCollected"),
        not_exposed_by_runtime: this.t("reasonNotExposed"),
        not_recorded_by_habab: this.t("reasonNotRecordedByHabab"),
      };
      if (!available && source?.reason) name.append(el("small", "source-reason", reasonLabels[source.reason] || this.t("sourceUnavailable")));
      item.append(name);
      item.append(el("span", "source-state", available ? this.t("available") : this.t("unavailable")));
      list.append(item);
    }
  }

  canExportPeriod() {
    try {
      const normalized = normalizeDiagnosticsFilters(this.filters);
      return Boolean(normalized.from && normalized.to);
    } catch (_) { return false; }
  }

  updateControls() {
    const selected = this.q("#download-session");
    const period = this.q("#download-period");
    const loadMore = this.q("#load-more");
    if (!selected || !period || !loadMore) return;
    selected.disabled = !this.selectedSession || this.exporting || this._hass?.user?.is_admin !== true;
    period.disabled = !this.canExportPeriod() || this.exporting || this._hass?.user?.is_admin !== true;
    loadMore.hidden = !this.cursor;
    loadMore.disabled = this.loading;
  }

  async downloadSelectedSession() {
    if (!this.selectedSession) return;
    const channel = this.filters.channel === "voice" ? "voice" : this.selectedSession.channel;
    await this.downloadArchive({session_id: this.selectedSession.session_id, channel});
  }

  async downloadPeriod() {
    let period;
    try { period = normalizeDiagnosticsFilters(this.filters); }
    catch (_) { this.renderStatus(this.t("invalidFilters"), true); return; }
    if (!period.from || !period.to) {
      this.renderStatus(this.t("periodHelp"), true);
      return;
    }
    await this.downloadArchive({from: period.from, to: period.to, ...(period.channel !== "all" ? {channel: period.channel} : {})});
  }

  async downloadArchive(scope) {
    if (this._hass?.user?.is_admin !== true || this.exporting) return;
    this.exportAbort?.abort();
    const controller = new AbortController();
    this.exportAbort = controller;
    this.exporting = true;
    this.updateControls();
    this.renderStatus(this.t("downloading"));
    try {
      const response = await this._hass.fetchWithAuth("/api/habab_home/diagnostics/export", {
        method: "POST", cache: "no-store", signal: controller.signal,
        headers: {"Content-Type": "application/json", Accept: "application/zip"},
        body: JSON.stringify(scope),
      });
      if (!response.ok) {
        const error = new Error("diagnostics_export_failed");
        error.status = response.status;
        throw error;
      }
      if (!response.headers?.get("content-type")?.toLowerCase().includes("application/zip")) throw new Error("diagnostics_invalid_response");
      const blob = await response.blob();
      if (!blob.size) throw new Error("diagnostics_invalid_response");
      if (this.exportObjectUrl) URL.revokeObjectURL(this.exportObjectUrl);
      const objectUrl = URL.createObjectURL(blob);
      this.exportObjectUrl = objectUrl;
      const anchor = el("a");
      anchor.href = objectUrl;
      anchor.download = this.downloadName(response.headers?.get("content-disposition"));
      anchor.hidden = true;
      this.shadowRoot.append(anchor);
      anchor.click();
      anchor.remove();
      this.exportTimer = setTimeout(() => {
        URL.revokeObjectURL(objectUrl);
        if (this.exportObjectUrl === objectUrl) this.exportObjectUrl = undefined;
      }, 60000);
      this.renderStatus(this.t("downloadStarted"));
    } catch (error) {
      if (controller.signal.aborted) return;
      this.renderStatus(error?.status === 401 ? this.t("unauthorized")
        : error?.status === 403 ? this.t("accessDenied")
          : error?.message === "diagnostics_invalid_response" ? this.t("invalidResponse")
            : this.t("exportError"), true);
    } finally {
      if (this.exportAbort === controller) this.exportAbort = undefined;
      this.exporting = false;
      this.updateControls();
    }
  }

  downloadName(disposition) {
    const match = typeof disposition === "string" ? disposition.match(/filename="([A-Za-z0-9._-]{1,80})"/i) : undefined;
    return match?.[1] || "habab-diagnostics.zip";
  }
}

if (typeof customElements !== "undefined" && !customElements.get("habab-diagnostics-panel")) {
  customElements.define("habab-diagnostics-panel", HababDiagnosticsPanel);
}
