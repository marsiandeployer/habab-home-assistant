// Read-only public demo panel. The private Home Assistant installation owns
// real Telegram setup; this panel has no token field or connection actions.
if (!customElements.get("habab-telegram-demo-panel")) {
  class HababTelegramDemoPanel extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
    }

    set hass(value) {
      this._hass = value;
      const menu = this.shadowRoot.querySelector("ha-menu-button");
      if (menu) menu.hass = value;
    }

    connectedCallback() {
      if (this.shadowRoot.firstChild) return;
      this.shadowRoot.innerHTML = `<style>
        :host{display:block;min-height:100%;overflow:auto;color:var(--primary-text-color);background:var(--primary-background-color);font:14px/1.55 var(--paper-font-body1_-_font-family,Roboto,system-ui,sans-serif)}
        *{box-sizing:border-box}header{display:flex;align-items:center;gap:16px;height:64px;padding-inline:16px;border-block-end:1px solid var(--divider-color);background:var(--card-background-color)}
        h1{font-size:20px;font-weight:500;margin:0}main{max-width:760px;margin:auto;padding:32px 24px 64px}h2{font-size:24px;font-weight:500;margin:0 0 12px}p{max-width:68ch;margin:0 0 16px;color:var(--secondary-text-color)}
        .notice{margin-block-start:28px;padding:20px 0;border-block-start:1px solid var(--divider-color)}
        @media(max-width:600px){main{padding:24px 16px 48px}h2{font-size:22px}}
      </style>
      <header><ha-menu-button></ha-menu-button><h1>Telegram</h1></header>
      <main><section aria-labelledby="telegram-demo-title">
        <h2 id="telegram-demo-title">Подключение Telegram</h2>
        <p>Подключение бота доступно в домашней установке Habab Home.</p>
        <p>Публичная демонстрация работает с виртуальными устройствами и не запускает реального бота.</p>
        <div class="notice"><p>В домашней установке создайте бота через BotFather, затем откройте раздел Telegram и выполните привязку аккаунта по одноразовой ссылке.</p></div>
      </section></main>`;
      this.shadowRoot.querySelector("ha-menu-button").hass = this._hass;
    }
  }

  customElements.define("habab-telegram-demo-panel", HababTelegramDemoPanel);
}
