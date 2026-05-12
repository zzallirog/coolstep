/* <lang-switcher> — segmented pill in the masthead.
 *
 * 3 buttons (EN/RU/UK). Click → LangStore.set(code). Active button has the
 * accent color + filled background. Stays inside the masthead-pills row.
 *
 * No tile-base styles — it's a chrome control, not a tile. Light DOM-ish
 * via shadow root for style isolation. */
import { LitElement, html, css } from 'https://esm.sh/lit@3';
import { LangStore, LangController, LANGS } from '../i18n/lang-store.js';

class LangSwitcher extends LitElement {
  static styles = css`
    :host {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      height: 24px;
      padding: 2px;
      border-radius: 999px;
      background: color-mix(in srgb, var(--surface-2, #1b2437) 60%, transparent);
      border: 1px solid var(--border, rgba(255,255,255,0.07));
      font-family: var(--font-mono, monospace);
    }
    button {
      appearance: none;
      border: 0;
      background: transparent;
      color: var(--fg-muted, #8893aa);
      font-family: inherit;
      font-size: 11px;
      letter-spacing: 0.06em;
      font-weight: 600;
      padding: 0 8px;
      height: 18px;
      line-height: 18px;
      border-radius: 999px;
      cursor: pointer;
      transition: color 120ms ease, background 120ms ease;
    }
    button:hover { color: var(--fg, #e4e7ef); }
    button.active {
      background: color-mix(in srgb, var(--accent, #6aa5ff) 18%, transparent);
      color: var(--accent, #6aa5ff);
    }
    button:focus-visible {
      outline: 2px solid var(--accent, #6aa5ff);
      outline-offset: 1px;
    }
  `;

  constructor() {
    super();
    this._lang = new LangController(this);
  }

  _pick(code) {
    LangStore.set(code);
  }

  render() {
    const active = this._lang.code;
    return html`${LANGS.map((l) => html`
      <button
        class="${l.code === active ? 'active' : ''}"
        @click=${() => this._pick(l.code)}
        title="${l.name}"
        aria-pressed="${l.code === active}">${l.label}</button>
    `)}`;
  }
}

customElements.define('lang-switcher', LangSwitcher);
