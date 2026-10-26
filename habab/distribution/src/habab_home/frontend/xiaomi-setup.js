// Native-only owner UI. OAuth URLs and callback codes never enter chat messages.
const copy = {
  ru: {
    title: 'Подключение Xiaomi Home',
    install_required: 'Официальная интеграция ещё не установлена. Помощник скачает проверенную версию в Home Assistant.',
    license: 'Использую интеграцию лично и принимаю условия Xiaomi.', source: 'Исходный код и лицензия',
    install: 'Установить интеграцию', installing: 'Скачиваем и проверяем интеграцию…',
    restart_required: 'Интеграция установлена. Перезапустите Home Assistant, чтобы продолжить. Настройки и история чата сохранятся.',
    restart: 'Перезапустить и продолжить', restarting: 'Home Assistant перезапускается. Ждём подключения…',
    restart_failed: 'Home Assistant не перезапустился. Проверьте конфигурацию с помощником и повторите перезапуск.',
    ready: 'Выберите тот же регион, что в приложении Xiaomi Home. Пароль вводится только на сайте Xiaomi.',
    region: 'Регион Xiaomi Home', cn: 'Китай', de: 'Европа', i2: 'Индия', ru: 'Россия', sg: 'Сингапур', us: 'США',
    begin: 'Подготовить вход Xiaomi', oauth: 'Откройте сайт Xiaomi, подтвердите вход и вернитесь в этот чат.',
    login: 'Войти в Xiaomi', callbackHelp: 'После входа страница не открылась?',
    callbackInstructions: 'Скопируйте полный адрес из адресной строки открывшейся страницы homeassistant.local и вставьте сюда. Он отправится только в Home Assistant, без передачи AI-модели.',
    callbackLabel: 'Адрес страницы после входа', callbackSubmit: 'Завершить вход',
    homes: 'Выберите дома для подключения.', selectHomes: 'Подключить выбранные дома',
    configuring: 'Home Assistant добавляет устройства…', completed: 'Xiaomi Home подключён.',
    dismissCompleted: 'Скрыть подключение Xiaomi',
    counts: 'Устройств: {devices}. Сущностей в реестре: {entities}.', verify: 'Проверить с помощником', openDevices: 'Показать пылесос на главной',
    error: 'Подключение не завершено.', retry: 'Повторить подключение',
    installation_failed: 'Не удалось скачать или проверить интеграцию. Повторите установку.',
    invalid_callback: 'Адрес не соответствует этому подключению. Скопируйте полный адрес страницы после входа Xiaomi.',
    no_devices_in_region: 'В выбранном регионе не найдены устройства. Проверьте регион в приложении Xiaomi Home.',
    integration_not_loaded: 'Интеграция создана, но не загрузилась. Помощник может проверить её состояние.',
    license_required: 'Подтвердите условия интеграции.', invalid_homes: 'Выберите хотя бы один доступный дом.',
    flow_failed: 'Xiaomi не завершил подключение. Можно начать вход заново.',
    setup_failed: 'Действие не завершилось. Проверьте состояние и повторите его.',
    disconnected: 'Нет связи с Home Assistant. Состояние обновится после подключения.',
  },
  en: {
    title: 'Connect Xiaomi Home',
    install_required: 'The official integration is not installed yet. The assistant will download a verified version into Home Assistant.',
    license: 'I use the integration personally and accept the Xiaomi terms.', source: 'Source code and license',
    install: 'Install integration', installing: 'Downloading and verifying the integration…',
    restart_required: 'The integration is installed. Restart Home Assistant to continue. Your settings and chat history will be retained.',
    restart: 'Restart and continue', restarting: 'Home Assistant is restarting. Waiting for reconnection…',
    restart_failed: 'Home Assistant did not restart. Check the configuration with the assistant and try again.',
    ready: 'Choose the region used in your Xiaomi Home app. Enter your password only on the Xiaomi website.',
    region: 'Xiaomi Home region', cn: 'China', de: 'Europe', i2: 'India', ru: 'Russia', sg: 'Singapore', us: 'United States',
    begin: 'Prepare Xiaomi sign-in', oauth: 'Open Xiaomi, confirm sign-in, and return to this chat.',
    login: 'Sign in to Xiaomi', callbackHelp: 'The page did not open after sign-in?',
    callbackInstructions: 'Copy the full address from the homeassistant.local page that opened and paste it here. It is sent only to Home Assistant, never to the AI model.',
    callbackLabel: 'Page address after sign-in', callbackSubmit: 'Complete sign-in',
    homes: 'Choose the homes to connect.', selectHomes: 'Connect selected homes',
    configuring: 'Home Assistant is adding devices…', completed: 'Xiaomi Home is connected.',
    dismissCompleted: 'Hide Xiaomi setup',
    counts: 'Devices: {devices}. Registered entities: {entities}.', verify: 'Check with the assistant', openDevices: 'Show vacuum on Home',
    error: 'Setup is incomplete.', retry: 'Retry sign-in',
    installation_failed: 'The integration could not be downloaded or verified. Retry installation.',
    invalid_callback: 'This address does not match your setup. Copy the full page address after Xiaomi sign-in.',
    no_devices_in_region: 'No devices were found in this region. Check the region in your Xiaomi Home app.',
    integration_not_loaded: 'The integration was created but did not load. The assistant can check its state.',
    license_required: 'Accept the integration terms.', invalid_homes: 'Select at least one available home.',
    flow_failed: 'Xiaomi setup did not finish. You can start sign-in again.',
    setup_failed: 'The action did not finish. Check its state and try again.',
    disconnected: 'Home Assistant is disconnected. The state will refresh after reconnection.',
  },
};
const pollingPhases = new Set(['installing', 'restarting', 'oauth', 'configuring']);

class HababXiaomiSetup extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: 'open'});
    this.state = {phase: 'idle'};
    this.busy = false;
  }
  set hass(value) {
    this._hass = value;
    const language = value?.locale.language.startsWith('ru') ? 'ru' : 'en';
    if (this.language !== language) {this.language = language; this.render();}
  }
  t(key) { return copy[this.language || 'en'][key] || copy[this.language || 'en'].setup_failed; }
  q(selector) { return this.shadowRoot.querySelector(selector); }
  connectedCallback() { if (pollingPhases.has(this.state.phase)) this.schedule(); }
  disconnectedCallback() {clearTimeout(this.timer);}
  async load() {
    if (!this._hass || this.loading) return;
    this.loading = true;
    try {
      const state = await this._hass.callWS({type: 'habab_home/xiaomi/get'});
      if (JSON.stringify(state) !== JSON.stringify(this.state)) {this.state = state; this.render();}
      this.dispatchEvent(new CustomEvent('habab-setup-state', {detail: {phase: state.phase}}));
    } catch (error) {
      if (this.state.phase !== 'restarting') this.showError(error.code || 'disconnected');
    } finally {this.loading = false; this.schedule();}
  }
  schedule() {
    clearTimeout(this.timer);
    if (this.isConnected && pollingPhases.has(this.state.phase)) {
      this.timer = setTimeout(() => this.load(), 2500);
    }
  }
  showError(code) {
    if (this.q('#feedback')) this.q('#feedback').textContent = code ? this.t(code) : '';
  }
  controls() {
    this.shadowRoot.querySelectorAll('button,input,select').forEach(el => {el.disabled = this.busy;});
    const accept = this.q('#accept');
    const action = this.q('#main');
    if (accept && action) action.disabled = this.busy || !accept.checked;
  }
  async act(action, data = {}) {
    if (this.busy) return;
    this.busy = true; this.controls(); this.showError('');
    try {
      this.state = await this._hass.callWS({type: 'habab_home/xiaomi/act', action, ...data});
      this.render();
      this.dispatchEvent(new CustomEvent('habab-setup-state', {detail: {phase: this.state.phase}}));
    } catch (error) {this.showError(error.code || 'setup_failed');}
    finally {this.busy = false; this.controls(); this.schedule();}
  }
  render() {
    const phase = this.state.phase;
    this.hidden = phase === 'idle';
    if (this.hidden) {this.shadowRoot.replaceChildren(); return;}
    const accept = `<label class="accept"><input id="accept" type="checkbox"><span>${this.t('license')} <a href="https://github.com/XiaoMi/ha_xiaomi_home/blob/75b3acb6325a3a8d295688b13904737d6738a6a5/LICENSE.md" target="_blank" rel="noopener noreferrer">${this.t('source')}</a></span></label>`;
    let body = '';
    if (phase === 'install_required') body = `${accept}<button id="main" type="button">${this.t('install')}</button>`;
    else if (phase === 'restart_required') body = `<button id="main" type="button">${this.t('restart')}</button>`;
    else if (phase === 'ready' || phase === 'error') {
      body = `<label>${this.t('region')}<select id="region">${['ru','de','cn','i2','sg','us'].map(r=>`<option value="${r}">${this.t(r)}</option>`).join('')}</select></label>${accept}<button id="main" type="button">${this.t('begin')}</button>`;
    } else if (phase === 'oauth') {
      body = `<a id="login" class="button" target="_blank" rel="noopener noreferrer">${this.t('login')}</a><details><summary>${this.t('callbackHelp')}</summary><p>${this.t('callbackInstructions')}</p><label for="callback">${this.t('callbackLabel')}</label><input id="callback" type="password" autocomplete="off" spellcheck="false"><button id="callback-submit" type="button">${this.t('callbackSubmit')}</button></details>`;
    } else if (phase === 'homes') body = `<div id="homes"></div><button id="main" type="button">${this.t('selectHomes')}</button>`;
    else if (phase === 'completed') body = `<p id="counts"></p><div class="actions"><button id="open-devices" type="button">${this.t('openDevices')}</button><button id="verify" type="button">${this.t('verify')}</button></div>`;
    const dismiss = canDismissXiaomiSetup(phase)
      ? `<button id="dismiss" class="dismiss" type="button" aria-label="${this.t('dismissCompleted')}" title="${this.t('dismissCompleted')}">×</button>`
      : '';
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;margin-block:20px;color:var(--primary-text-color);font:inherit}*{box-sizing:border-box}
      section{padding:16px;border:1px solid var(--divider-color,#ddd);border-radius:12px;background:var(--card-background-color,#fff)}
      .heading{display:flex;align-items:flex-start;gap:8px}h3{font-size:16px;margin:0 0 8px;flex:1}p{margin:8px 0;overflow-wrap:anywhere}label{display:block;margin:12px 0}.accept{display:flex;align-items:start;gap:10px}.accept input{width:20px;min-width:20px;height:20px;margin-top:2px}
      button,.button,select,input{font:inherit;min-height:44px}button,.button{display:inline-flex;align-items:center;padding:8px 14px;border:1px solid var(--divider-color,#ddd);border-radius:8px;cursor:pointer;background:var(--primary-color,#007a9e);color:var(--text-primary-color,#fff);text-decoration:none}
      button.dismiss{min-width:44px;min-height:44px;padding:0 8px;border-color:transparent;background:transparent;color:var(--secondary-text-color,#5f6368);font-size:22px;line-height:1}
      button:disabled{opacity:.5;cursor:default}select,input[type=password]{width:100%;max-width:100%;padding:8px;background:var(--card-background-color,#fff);color:inherit;border:1px solid var(--divider-color,#ddd);border-radius:6px}a{color:var(--primary-color,#007a9e)}details{margin-top:16px}summary{cursor:pointer;min-height:44px;padding-block:10px}#feedback{color:var(--error-color,#b3261e)}:is(button,a,input,select,summary):focus-visible{outline:3px solid var(--primary-color,#007a9e);outline-offset:3px}
    </style><section aria-labelledby="title"><div class="heading"><h3 id="title">${this.t('title')}</h3>${dismiss}</div><p>${this.t(phase)}</p>${body}<p id="feedback" role="status" aria-live="polite"></p></section>`;
    const main = this.q('#main');
    if (phase === 'install_required') main.onclick = () => this.act('install', {accept_license: this.q('#accept').checked});
    else if (phase === 'restart_required') main.onclick = () => this.act('restart');
    else if (phase === 'ready' || phase === 'error') {
      this.q('#region').value = this.state.region || 'ru';
      main.onclick = () => this.act('begin', {region: this.q('#region').value, language: this.language, accept_license: this.q('#accept').checked});
    } else if (phase === 'oauth') {
      this.q('#login').href = this.state.authorization_url;
      this.q('#callback-submit').onclick = () => {
        const field = this.q('#callback'); const value = field.value; field.value = '';
        this.act('callback', {callback_url: value});
      };
    } else if (phase === 'homes') {
      for (const home of this.state.homes) {
        const label = document.createElement('label'), input = document.createElement('input'), text = document.createElement('span');
        input.type = 'checkbox'; input.value = home.value; text.textContent = home.label; label.append(input,text); this.q('#homes').append(label);
      }
      main.onclick = () => this.act('homes', {homes: [...this.q('#homes').querySelectorAll('input:checked')].map(x=>x.value)});
    } else if (phase === 'completed') {
      this.q('#counts').textContent = this.t('counts').replace('{devices}', String(this.state.device_count)).replace('{entities}', String(this.state.entity_count));
      this.q('#open-devices').onclick = () => this.dispatchEvent(new CustomEvent('habab-open-devices'));
      this.q('#verify').onclick = () => this.dispatchEvent(new CustomEvent('habab-setup-verify'));
    }
    const dismissButton = this.q('#dismiss');
    if (dismissButton) dismissButton.onclick = () => this.dismissCompleted();
    if (this.q('#accept')) this.q('#accept').onchange = () => this.controls();
    if (this.state.error) this.showError(this.state.error);
    this.controls();
  }

  dismissCompleted() {
    if (!canDismissXiaomiSetup(this.state?.phase)) return;
    this.dispatchEvent(new CustomEvent('habab-setup-dismiss', {bubbles: true, composed: true}));
  }
}
export function canDismissXiaomiSetup(phase) { return phase === 'completed'; }
if (!customElements.get('habab-xiaomi-setup')) customElements.define('habab-xiaomi-setup', HababXiaomiSetup);
