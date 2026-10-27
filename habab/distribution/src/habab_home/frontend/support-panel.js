const SUPPORT_URL = "https://t.me/+zdcn-KkegHg4Mjgy";

const copy = {
  en: {
    title: "Habab Home support (beta)",
    intro: "Questions about installation, devices, or setup are welcome. Beta feedback is welcome too.",
    open: "Open “Habab Home — поддержка (бета)” in Telegram",
    safety: "Group messages are visible to all members. Do not share passwords, API keys, tokens, Wi-Fi access codes, or other private details about your home.",
  },
  ru: {
    title: "Поддержка Habab Home (бета)",
    intro: "Здесь можно задать вопрос об установке, устройствах и настройке. Будем рады отзывам о бета-версии.",
    open: "Открыть «Habab Home — поддержка (бета)» в Telegram",
    safety: "Сообщения группы видны всем её участникам. Не публикуйте пароли, API-ключи, токены, коды доступа к Wi-Fi или другие личные данные о доме.",
  },
};

class HababSupportPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set hass(value) {
    this._hass = value;
    const language = value?.locale?.language?.startsWith("ru") ? "ru" : "en";
    if (!this.shadowRoot.firstChild || language !== this.language) {
      this.language = language;
      this.render();
    }
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.hass = value;
  }

  connectedCallback() {
    if (!this.shadowRoot.firstChild) this.render();
  }

  render() {
    const text = copy[this.language || "en"];
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;min-height:100%;color:var(--primary-text-color);background:var(--primary-background-color);font:14px/1.55 var(--paper-font-body1_-_font-family,Roboto,system-ui,sans-serif)}
      *{box-sizing:border-box}header{display:flex;align-items:center;gap:var(--ha-space-4,16px);min-height:56px;padding:8px 16px;border-block-end:1px solid var(--divider-color);background:var(--card-background-color)}
      ha-menu-button{flex:0 0 48px;width:48px;height:48px;min-width:48px;min-height:48px;--ha-icon-button-size:48px}
      main{max-width:760px;margin:0 auto;padding:var(--ha-space-6,24px)}
      section{padding:var(--ha-space-6,24px);background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:12px}
      h1{font-size:22px;font-weight:500;line-height:1.3;margin:0 0 var(--ha-space-2,8px)}
      header h1{font-size:20px;font-weight:500;margin:0}
      p{max-width:65ch;margin:0 0 var(--ha-space-4,16px);color:var(--secondary-text-color)}
      a{display:inline-flex;align-items:center;justify-content:center;min-height:48px;padding:10px 16px;border-radius:8px;background:var(--ha-color-fill-primary-loud-hover,var(--dark-primary-color));color:var(--ha-color-on-primary-loud,var(--text-primary-color));font:inherit;font-weight:500;text-align:center;text-decoration:none}
      a:hover{filter:brightness(1.08)}a:active{filter:brightness(.94)}a:focus-visible{outline:3px solid var(--ha-color-focus,var(--primary-color));outline-offset:3px}
      .safety{margin:var(--ha-space-4,16px) 0 0;padding:12px;border-inline-start:3px solid var(--primary-color);background:var(--secondary-background-color);color:var(--primary-text-color)}
      @media(max-width:600px){header{min-height:52px}main{padding:var(--ha-space-4,16px)}section{padding:var(--ha-space-4,16px)}a{width:100%}}
    </style>
    <header><ha-menu-button></ha-menu-button><h1 id="support-title">${text.title}</h1></header>
    <main>
      <section aria-labelledby="support-title">
        <p>${text.intro}</p>
        <a href="${SUPPORT_URL}" target="_blank" rel="noopener noreferrer">${text.open}</a>
        <p class="safety">${text.safety}</p>
      </section>
    </main>`;
  }
}

if (!customElements.get("habab-support-panel")) {
  customElements.define("habab-support-panel", HababSupportPanel);
}
