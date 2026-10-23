// Independent downstream extension: no dependency on compiled HA internals.
export function visibleTextMessages(messages) {
  if (!Array.isArray(messages)) return [];
  return messages.filter((message) =>
    typeof message?.text === "string" && message.text.trim().length > 0
  );
}

function installChat() {
  // HA installs its scoped registry polyfill before defining home-assistant.
  // Registering earlier loses the extension when that registry is replaced.
  if (customElements.get("habab-setup-chat")) return;
  const copy = {
    ru: {
      title: "Помощник", open: "Открыть чат с помощником", close: "Закрыть", fresh: "Новый чат",
      intro: "Что подключим к вашему дому?", help: "Помогу найти устройство, проверить подключение и настроить Home Assistant.",
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
    },
    en: {
      title: "Assistant", open: "Open assistant chat", close: "Close", fresh: "New chat",
      intro: "What shall we connect?", help: "Find devices, check connections and set up Home Assistant.",
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
    },
  };

  class HababSetupChat extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({mode: "open"});
      this.messages = [];
      this.busy = false;
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
    }

    get t() { return copy[this.language || "en"]; }
    q(selector) { return this.shadowRoot.querySelector(selector); }

    async loadSetup() {
      if (this.demo) return;
      try {
        await import("/habab-home/xiaomi-setup.js?v=2");
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
        this.setupCard.hass = this.hass;
        this.q("#log").append(this.setupCard);
        await this.setupCard.load();
        this.setupCard.scrollIntoView({block: "nearest"});
      } catch (_) { this.status(this.t.setup_ui_unavailable, true); }
    }

    connectedCallback() {
      if (this.shadowRoot.firstChild) return;
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
        @media(max-width:600px){:host{inset:0;z-index:1000}.launcher{position:fixed;inset-inline-end:max(16px,env(safe-area-inset-right));bottom:max(16px,env(safe-area-inset-bottom))}dialog{inset:0;width:100vw;height:100dvh;border-radius:0;margin:0}header{padding:8px 12px}#log{padding:20px 16px}footer{padding:12px 12px max(16px,env(safe-area-inset-bottom))}.empty h3{font-size:24px}.fresh{padding-inline:8px}.message.user{max-width:92%}}
        @media(prefers-reduced-motion:no-preference){dialog[open]{animation:arrive 160ms cubic-bezier(.16,1,.3,1)}@keyframes arrive{from{transform:translateY(10px);opacity:.7}to{transform:translateY(0);opacity:1}}}
      </style>
      <button class="icon launcher" type="button" title="${t.open}" aria-label="${t.open}" aria-haspopup="dialog"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11.5a7.5 7.5 0 0 1-7.5 7.5H5l-3 3V11.5A7.5 7.5 0 0 1 9.5 4h3A7.5 7.5 0 0 1 20 11.5Z"/><path d="M7 10h8M7 14h5"/></svg></button>
      <dialog aria-labelledby="chat-title"><div class="layout"><header><h2 id="chat-title">${t.title}</h2><button class="fresh" type="button">${t.fresh}</button><button class="icon close" type="button" aria-label="${t.close}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button></header>
      <div id="log" role="log" aria-live="polite" aria-label="${t.title}"></div><footer><p id="status" role="status"></p><div class="recovery" hidden><a href="/habab-ai">${t.settings}</a><button type="button" class="retry">${t.retry}</button></div>
      <form><textarea aria-label="${t.placeholder}" placeholder="${t.placeholder}" maxlength="4000" rows="2"></textarea><button class="send icon" type="submit" aria-label="${t.send}" title="${t.send}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg></button><button class="stop" type="button" hidden>${t.stop}</button></form></footer></div></dialog>`;
      this.q(".launcher").onclick = () => this.open();
      this.q(".close").onclick = () => this.q("dialog").close();
      this.q("dialog").addEventListener("close", () => this.q(".launcher").focus());
      this.q(".fresh").onclick = () => this.connect(true);
      this.q(".retry").onclick = () => this.connect();
      this.q("form").onsubmit = (event) => {event.preventDefault(); this.send();};
      this.q("textarea").onkeydown = (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {event.preventDefault(); this.send();}
      };
      this.q(".stop").onclick = () => this.stop();
      this.draw();
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
      this.q("#log").setAttribute("aria-label", t.title);
      const textarea = this.q("textarea");
      textarea.placeholder = t.placeholder;
      textarea.setAttribute("aria-label", t.placeholder);
      this.q(".recovery a").textContent = t.settings;
      this.q(".retry").textContent = t.retry;
      this.q(".send").title = t.send;
      this.q(".send").setAttribute("aria-label", t.send);
      this.q(".stop").textContent = t.stop;
      this.draw();
    }

    async open() {
      const dialog = this.q("dialog");
      if (matchMedia("(max-width: 600px)").matches) dialog.showModal();
      else dialog.show();
      this.q("textarea").focus();
      if (!this.sessionId && !this.busy) await this.connect();
    }

    status(text, error = false) {
      this.q("#status").textContent = text;
      this.q("#status").classList.toggle("error", error);
      this.q(".recovery").hidden = !error;
      this.q(".recovery a").hidden = this.demo;
    }

    controls() {
      this.q("textarea").disabled = !this.sessionId || this.busy || this.setupRestarting;
      this.q(".send").disabled = !this.sessionId || this.busy || this.setupRestarting;
      this.q(".send").hidden = this.busy;
      this.q(".stop").hidden = !this.running || this.demo;
      this.q(".fresh").disabled = this.busy || !this.sessionId;
      this.q(".retry").disabled = this.busy;
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
      finally { this.busy = false; this.controls(); if (this.q("dialog").open) this.q("textarea").focus(); }
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

    draw() {
      const log = this.q("#log");
      log.replaceChildren();
      const visibleMessages = visibleTextMessages(this.messages);
      if (!visibleMessages.length) {
        const empty = document.createElement("div"); empty.className = "empty";
        const title = document.createElement("h3"); title.textContent = this.t.intro;
        const help = document.createElement("p"); help.textContent = this.t.help;
        empty.append(title, help); log.append(empty);
      }
      for (const message of visibleMessages) {
        const row = document.createElement("div"); row.className = `message ${message.role}`;
        const name = document.createElement("div"); name.className = "name"; name.textContent = message.role === "user" ? this.t.you : this.t.assistant;
        const markdown = message.role === "assistant" && customElements.get("ha-markdown");
        const text = document.createElement(markdown ? "ha-markdown" : "p");
        if (markdown) {text.content = message.text; text.breaks = true;}
        else text.textContent = message.text;
        row.append(name, text); log.append(row);
      }
      if (this.setupCard) log.append(this.setupCard);
      log.scrollTop = log.scrollHeight;
      const tail = log.lastChild;
      Promise.allSettled([...log.querySelectorAll("ha-markdown")].map(item => item.updateComplete)).then(() => {
        if (log.lastChild === tail) log.scrollTop = log.scrollHeight;
      });
    }

    async send() {
      const text = this.q("textarea").value.trim();
      if (!text || !this.sessionId || this.busy || this.setupRestarting) return;
      this.busy = true; this.running = true; this.stopped = false;
      this.messages.push({role: "user", text}); this.q("textarea").value = "";
      this.draw(); this.status(this.demo ? this.t.answering : this.t.working); this.controls();
      try {
        const result = this.demo ? await this.demoRequest("", "POST", text)
          : await this.hass.callWS({type: "habab_home/agent/run", session_id: this.sessionId, text});
        this.messages = result.messages; this.draw(); this.status(this.stopped ? this.t.stopped : this.demo ? this.t.demo : "");
        await this.loadSetup();
      } catch (error) { this.status(this.stopped ? this.t.stopped : this.t[error.code] || this.t.failed, true); }
      finally { this.busy = false; this.running = false; this.controls(); if (this.q("dialog").open) this.q("textarea").focus(); }
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
  const syncLauncher = () => {
    const main = locateMain(document);
    const demo = document.documentElement.dataset.hababDemo === "true";
    const hass = main?.hass;
    if (!demo && !hass?.user?.is_admin) {launcher?.remove(); launcher = undefined; return;}
    if (!demo && !main) {launcher?.remove(); launcher = undefined; return;}
    if (!launcher) {
      launcher = document.createElement("habab-setup-chat");
      launcher.demo = demo;
      launcher.hass = hass;
      document.body.append(launcher);
    }
    launcher.hass = hass;
    syncThemeVariables(launcher, main);
    if (launcher.parentNode !== document.body) document.body.append(launcher);
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
