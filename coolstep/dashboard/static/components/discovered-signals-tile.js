import { LitElement, html, css, fetchJson, tileBaseStyles, renderFrame } from './_base.js';

export class DiscoveredSignalsTile extends LitElement {
  static styles = [
    tileBaseStyles,
    css`
      :host { grid-column: span 2; }
      .filter { display: flex; gap: var(--sp-1); flex-wrap: wrap; }
      .chip {
        padding: 4px 12px;
        border-radius: var(--radius-pill);
        background: color-mix(in srgb, var(--surface-2) 50%, transparent);
        border: 1px solid var(--border);
        font-family: var(--font-mono);
        font-size: 11px;
        cursor: pointer;
        color: var(--fg-muted);
        transition: all var(--dur-fast) var(--ease-out);
      }
      .chip:hover { color: var(--fg); border-color: var(--border-strong); }
      .chip.active {
        background: color-mix(in srgb, var(--cat-color) 14%, transparent);
        color: var(--cat-color);
        border-color: color-mix(in srgb, var(--cat-color) 50%, transparent);
      }
      table { width: 100%; font-size: 12px; border-collapse: collapse; font-family: var(--font-mono); }
      th { color: var(--fg-muted); font-weight: 600; text-align: left;
           padding: 6px 8px 6px 0; border-bottom: 1px solid var(--border);
           font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; }
      td { padding: 5px 8px 5px 0; border-bottom: 1px dotted var(--border-soft); vertical-align: top; }
      .name { color: var(--fg); }
      .unit { color: var(--cat-color); }
      .source { color: var(--fg-muted); font-size: 11px; max-width: 380px; word-break: break-all; }
    `,
  ];

  static properties = {
    signals: { state: true },
    activeCollector: { state: true },
  };

  constructor() {
    super();
    this.signals = [];
    this.activeCollector = null;
  }

  async connectedCallback() {
    super.connectedCallback();
    const data = await fetchJson('/api/discoveries', { signals: [], total: 0 });
    this.signals = data.signals || [];
  }

  _collectors() {
    return [...new Set(this.signals.map((s) => s.collector))].sort();
  }

  _filtered() {
    if (!this.activeCollector) return this.signals;
    return this.signals.filter((s) => s.collector === this.activeCollector);
  }

  render() {
    const filtered = this._filtered();
    const collectors = this._collectors();
    return renderFrame({
      title: 'Discovered signals',
      meta: `${this.signals.length}`,
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">total signals</span>
            <span class="metric cat">${this.signals.length}</span>
          </div>
          <div class="col">
            <span class="eyebrow">collectors</span>
            <span class="metric">${collectors.length}</span>
          </div>
          <div class="aside">
            <span class="eyebrow">filter</span>
            <span class="stat-pill ${this.activeCollector ? 'cat' : ''}">
              ${this.activeCollector ?? 'all'}
            </span>
          </div>
        </div>

        <div class="filter">
          <span class="chip ${!this.activeCollector ? 'active' : ''}"
                @click=${() => (this.activeCollector = null)}>all</span>
          ${collectors.map(
            (c) => html`
              <span class="chip ${this.activeCollector === c ? 'active' : ''}"
                    @click=${() => (this.activeCollector = c)}>${c}</span>
            `
          )}
        </div>
        <table>
          <thead>
            <tr><th>name</th><th>unit</th><th>dtype</th><th>source</th></tr>
          </thead>
          <tbody>
            ${filtered.map(
              (s) => html`
                <tr>
                  <td class="name">${s.name}</td>
                  <td class="unit">${s.unit}</td>
                  <td>${s.dtype}</td>
                  <td class="source">${s.source}</td>
                </tr>
              `
            )}
          </tbody>
        </table>
        <p class="muted">
          Zabbix-LLD-style manifest. Каждый collector декларирует свои signals;
          research-tools и dashboard читают вместо хардкода.
        </p>
      `,
    });
  }
}

customElements.define('discovered-signals-tile', DiscoveredSignalsTile);
