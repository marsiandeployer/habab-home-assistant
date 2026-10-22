// Habab demo integration. Bot tokens never leave this form or enter storage.
(() => {
  if (customElements.get('habab-telegram-setup')) return;
  class TelegramSetup extends HTMLElement {
    connectedCallback() {
      if (this.shadowRoot) return;
      const root = this.attachShadow({mode: 'open'});
      root.innerHTML = `<style>
        :host{--accent:#006b8f;font:14px/1.5 Roboto,system-ui,sans-serif;color:#212121;position:fixed;inset-block-end:max(20px,env(safe-area-inset-bottom));inset-inline-end:20px;z-index:1000}
        *{box-sizing:border-box}button,input{font:inherit}button{cursor:pointer;min-height:44px;border-radius:8px;padding:9px 16px;border:1px solid #b8cbd2;background:#fff;color:var(--accent)}
        button:hover{background:#eef6f9}button:active{background:#dcecf2}button:focus-visible,input:focus-visible{outline:3px solid #006b8f;outline-offset:3px}
        .launcher{display:flex;align-items:center;gap:9px;background:#fff;border-color:#c6d7dd}svg{width:22px;height:22px;flex:none}
        dialog{border:1px solid #d5dde1;border-radius:12px;width:min(460px,calc(100vw - 24px));max-height:calc(100dvh - 24px);overflow:auto;padding:24px;color:#212121;background:#fff;margin:auto}
        dialog::backdrop{background:#0007}header{display:flex;align-items:start;gap:14px}header svg{width:28px;height:28px;margin-block-start:3px;color:var(--accent)}h2{font-size:22px;line-height:1.3;margin:0;font-weight:500}p{margin:12px 0;color:#4e5961}.demo{font-size:12px;margin:4px 0 0;color:#586873}
        form{margin-block-start:24px}label{display:block;font-weight:500;margin-block-end:8px}.field{display:flex;gap:8px;align-items:center}input{width:100%;min-width:0;height:48px;padding:10px 12px;border:1px solid #7d8c95;border-radius:6px;background:#fff;color:#212121}input[aria-invalid=true]{border-color:#ad2020}.reveal{flex:none;padding-inline:12px}
        .help{font-size:13px;color:#56636c;margin:10px 0 0}.error{color:#ad2020;font-size:13px;margin:8px 0}.success{border-inline-start:3px solid #28783f;padding-inline-start:14px;margin-block:20px}.success strong{color:#245f35;font-weight:500}.success p{margin:6px 0 0}
        footer{display:flex;justify-content:flex-end;gap:10px;margin-block-start:24px}.primary{background:var(--accent);color:#fff;border-color:var(--accent)}.primary:hover{background:#005675}.primary:active{background:#00465f}.cancel{border-color:transparent;color:#45545e}[hidden]{display:none!important}
        @media(max-width:480px){:host{inset-inline-end:12px;inset-block-end:max(12px,env(safe-area-inset-bottom))}dialog{padding:20px}.field{flex-wrap:wrap}.field input{flex:1 1 100%}.reveal{min-height:40px}h2{font-size:21px}}
      </style>
      <button class="launcher" type="button" aria-haspopup="dialog"><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="m21 3-7 18-4-7-7-4 18-7Z"/><path d="m10 14 6-6"/></svg>Telegram</button>
      <dialog aria-labelledby="telegram-title" aria-describedby="telegram-description">
        <header><svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="m21 3-7 18-4-7-7-4 18-7Z"/><path d="m10 14 6-6"/></svg><div><h2 id="telegram-title">Подключить Telegram</h2><p class="demo">Демонстрация настройки</p></div></header>
        <p id="telegram-description">Чат с домашним помощником: вопросы и команды устройствам.</p>
        <form novalidate autocomplete="off">
          <label for="bot-token">API-токен бота</label>
          <div class="field"><input id="bot-token" type="password" required maxlength="200" autocomplete="off" autocapitalize="off" spellcheck="false" aria-describedby="token-help token-error"><button class="reveal" type="button" aria-pressed="false">Показать</button></div>
          <p class="help" id="token-help">В демо токен не отправляется и не сохраняется. Настоящий бот пока не запускается.</p>
          <p class="error" id="token-error" role="alert" hidden>Проверьте токен: номер бота, двоеточие и секретная часть.</p>
          <footer><button class="cancel" type="button">Отмена</button><button class="primary" type="submit">Подключить</button></footer>
        </form>
        <section class="success" role="status" hidden><strong>Демо-подключение готово</strong><p>Так будет выглядеть подключение вашего бота. Токен очищен, бот не запущен.</p></section>
        <footer class="finished" hidden><button class="primary close" type="button">Готово</button></footer>
      </dialog>`;
      const dialog = root.querySelector('dialog');
      const form = root.querySelector('form');
      const input = root.querySelector('input');
      const error = root.querySelector('.error');
      const reveal = root.querySelector('.reveal');
      const reset = () => {
        input.value = '';
        input.type = 'password';
        input.removeAttribute('aria-invalid');
        reveal.textContent = 'Показать';
        reveal.setAttribute('aria-pressed', 'false');
        error.hidden = true;
        form.hidden = false;
        root.querySelector('.success').hidden = true;
        root.querySelector('.finished').hidden = true;
      };
      root.querySelector('.launcher').addEventListener('click', () => {reset();dialog.showModal();input.focus();});
      const close = () => {reset();dialog.close();};
      root.querySelector('.cancel').addEventListener('click', close);
      root.querySelector('.close').addEventListener('click', close);
      dialog.addEventListener('close', reset);
      dialog.addEventListener('cancel', reset);
      input.addEventListener('input', () => {error.hidden = true;input.removeAttribute('aria-invalid');});
      reveal.addEventListener('click', () => {
        const show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        reveal.textContent = show ? 'Скрыть' : 'Показать';
        reveal.setAttribute('aria-pressed', String(show));
      });
      form.addEventListener('submit', event => {
        event.preventDefault();
        if (!/^\d{5,20}:[A-Za-z0-9_-]{20,100}$/.test(input.value.trim())) {
          error.hidden = false;input.setAttribute('aria-invalid', 'true');input.focus();return;
        }
        input.value = '';
        form.hidden = true;
        root.querySelector('.success').hidden = false;
        root.querySelector('.finished').hidden = false;
        root.querySelector('.close').focus();
      });
    }
  }
  customElements.define('habab-telegram-setup', TelegramSetup);
  document.body.appendChild(document.createElement('habab-telegram-setup'));
})();
