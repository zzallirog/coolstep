import { LitElement, html, css, fetchJson, tileBaseStyles, renderFrame } from './_base.js';
import { orchestrator } from './_orchestrator.js?v=mount-on-register';

export class AdaptersHealthTile extends LitElement {
  static get priority() { return 'normal'; }

  static styles = [
    tileBaseStyles,
    css`
      table { width: 100%; font-size: 12px; border-collapse: collapse;
              font-family: var(--font-mono, monospace); }
      th { color: var(--fg-muted); font-weight: 600; text-align: left;
           padding: 6px 8px 6px 0; border-bottom: 1px solid var(--border);
           font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; }
      td { padding: 5px 8px 5px 0; border-bottom: 1px dotted var(--border-soft);
           font-variant-numeric: tabular-nums; }
      td:nth-child(2), td:nth-child(3), td:nth-child(4) { text-align: right; }
      .hot  { color: var(--warn); font-weight: 500; }
      .cold { color: var(--ok); }
      .stale { color: var(--warn); }
      .empty { color: var(--fg-muted); }
      .verbs { color: var(--fg-muted); font-size: 11px; }
    `,
  ];

  static properties = {
    collectors: { state: true },
    actuators: { state: true },
  };

  constructor() {
    super();
    this.collectors = [];
    this.actuators = [];
  }

  connectedCallback() {
    super.connectedCallback();
    orchestrator.register('adapters-health-tile', {
      priority: 'normal',
      element: this,
      mountFn: () => this._mount(),
    });
  }

  _mount() {
    this._refresh();
    this._timer = setInterval(() => this._refresh(), 30000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
  }

  async _refresh() {
    const data = await orchestrator.fetchJson('/api/adapters', null);
    if (!data) return;  // hold prior on transient failure — avoid 0/0 flash
    this.collectors = data.collectors || [];
    this.actuators = data.actuators || [];
  }

  render() {
    const discovered = this.collectors.filter((c) => c.discovered).length;
    const total = this.collectors.length;
    const totalSignals = this.collectors.reduce((s, c) => s + (c.signal_count || 0), 0);
    const totalCostUs = this.collectors.reduce((s, c) => s + (c.sample_us || 0), 0);
    const healthCls = total === 0 ? 'warn' : discovered === total ? 'ok' : 'warn';
    const nowSec = Date.now() / 1000;
    const sampleState = (collector) => {
      if (!collector.last_nonempty_at) return { label: 'no data', cls: 'empty' };
      if ((nowSec - collector.last_nonempty_at) > 60) return { label: 'stale', cls: 'stale' };
      return {
        label: collector.sample_us,
        cls: collector.sample_us > 10000 ? 'hot' : 'cold',
      };
    };

    return renderFrame({
      title: 'Adapters health',
      meta: `${discovered}/${total}`,
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">collectors live</span>
            <span class="metric ${healthCls}">${discovered}<small>/ ${total}</small></span>
          </div>
          <div class="col">
            <span class="eyebrow">signals</span>
            <span class="metric">${totalSignals}</span>
          </div>
          <div class="aside">
            <span class="eyebrow">total cost</span>
            <span class="stat-pill ${totalCostUs > 50000 ? 'warn' : 'ok'}">${(totalCostUs / 1000).toFixed(1)}ms</span>
          </div>
        </div>

        <table>
          <thead>
            <tr><th>collector</th><th>discovered</th><th>sample μs</th><th>signals</th></tr>
          </thead>
          <tbody>
            ${this.collectors.length === 0
              ? html`<tr><td colspan="4" class="empty">No collectors registered.</td></tr>`
              : this.collectors.map(
                  (a) => {
                    const state = sampleState(a);
                    return html`
                      <tr>
                        <td>${a.name}</td>
                        <td>${a.discovered ? '✓' : '—'}</td>
                        <td class=${state.cls}>${state.label}</td>
                        <td>${a.signal_count ?? '—'}</td>
                      </tr>
                    `;
                  }
                )}
          </tbody>
        </table>

        <h3 class="section-h">Actuators · ${this.actuators.length}</h3>
        <table>
          <thead>
            <tr><th>actuator</th><th>discovered</th><th>verbs</th></tr>
          </thead>
          <tbody>
            ${this.actuators.length === 0
              ? html`<tr><td colspan="3" class="empty">No actuators registered.</td></tr>`
              : this.actuators.map(
                  (a) => html`
                    <tr>
                      <td>${a.name}</td>
                      <td>${a.discovered ? '✓' : '—'}</td>
                      <td class="verbs">${(a.supports || []).join(' · ')}</td>
                    </tr>
                  `
                )}
          </tbody>
        </table>
        <p class="muted">Cost = mean wall time per sample(). Self-pressure budget &lt; 1% CPU.</p>
      `,
    });
  }
}

customElements.define('adapters-health-tile', AdaptersHealthTile);
