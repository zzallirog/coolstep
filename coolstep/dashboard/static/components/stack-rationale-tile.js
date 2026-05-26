import { LitElement, html, css, fetchJson, tileBaseStyles, renderFrame } from './_base.js';
import { orchestrator } from './_orchestrator.js?v=mount-on-register';

export class StackRationaleTile extends LitElement {
  static get priority() { return 'lazy'; }
  static styles = [
    tileBaseStyles,
    css`
      .list { display: flex; flex-direction: column; gap: 0.4rem; max-height: 360px; overflow-y: auto; }
      details {
        background: #14181f;
        border: 1px solid var(--border, #232936);
        border-radius: 8px;
        padding: 0.5rem 0.75rem;
      }
      summary { cursor: pointer; font-size: 0.86rem; }
      .id { color: var(--accent, #7cc4ff); font-weight: 600; margin-right: 0.4rem; }
      .adr-body { margin: 0.4rem 0 0; color: var(--fg-muted, #8a93a4);
              font-size: 0.85rem; line-height: 1.45; white-space: pre-wrap; }
    `,
  ];

  static properties = {
    adrs: { state: true },
  };

  constructor() {
    super();
    this.adrs = [];
  }

  connectedCallback() {
    super.connectedCallback();
    orchestrator.register('stack-rationale-tile', {
      priority: 'lazy',
      element: this,
      mountFn: () => this._mount(),
    });
  }

  async _mount() {
    const data = await orchestrator.fetchJson('/api/stack-rationale', { adrs: [] });
    this.adrs = data.adrs || [];
  }

  render() {
    return renderFrame({
      title: 'Stack rationale',
      meta: `${this.adrs.length} ADRs`,
      body: html`
        <div class="list">
          ${this.adrs.map(
            (adr) => html`
              <details>
                <summary><span class="id">ADR-${adr.id}</span>${adr.title}</summary>
                <p class="adr-body">${adr.body}</p>
              </details>
            `
          )}
        </div>
        <p class="muted">source: docs/stack-decisions.md</p>
      `,
    });
  }
}

customElements.define('stack-rationale-tile', StackRationaleTile);
