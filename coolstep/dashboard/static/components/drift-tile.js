import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame } from './_base.js';

export class DriftTile extends LitElement {
  static styles = [
    tileBaseStyles,
    css`
      ul { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 6px; }
      li {
        padding: 8px 12px;
        border-radius: var(--radius-sm);
        font-size: 13px;
        border-left: 3px solid var(--border-strong);
        background: color-mix(in srgb, var(--surface-2) 38%, transparent);
        position: relative;
      }
      li.high { background: var(--err-soft);  border-left-color: var(--err); }
      li.mid  { background: var(--warn-soft); border-left-color: var(--warn); }
      li.low  { background: color-mix(in srgb, var(--surface-2) 50%, transparent); border-left-color: var(--cat-color); }
      li .name { font-family: var(--font-mono, monospace); color: var(--fg); font-weight: 500; }
      li .note { color: var(--fg-muted); font-size: 12px; margin-top: 4px; line-height: 1.4; }
      li .sev {
        position: absolute; right: 12px; top: 8px;
        font-family: var(--font-mono, monospace);
        font-size: 12px;
        color: var(--fg-muted);
        font-variant-numeric: tabular-nums;
      }
    `,
  ];

  static properties = {
    report: { state: true },
  };

  constructor() {
    super();
    this.report = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    this._timer = setInterval(() => this._refresh(), 30000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
  }

  async _refresh() {
    this.report = await fetchJson('/api/drift', { severity: 0, indicators: [] });
  }

  _bucketClass(sev) {
    if (sev >= 0.7) return 'high';
    if (sev >= 0.4) return 'mid';
    return 'low';
  }

  _severityHero(sev) {
    if (sev >= 0.7) return 'err';
    if (sev >= 0.4) return 'warn';
    return 'ok';
  }

  _severityLabel(sev) {
    if (sev >= 0.7) return 'alert';
    if (sev >= 0.4) return 'warn';
    return 'calm';
  }

  render() {
    const r = this.report || { severity: 0, indicators: [], samples_for_baseline: 0 };
    return renderFrame({
      title: 'Model drift',
      meta: `${r.samples_for_baseline} samples`,
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow">aggregate severity</span>
            <span class="metric ${this._severityHero(r.severity)}">${fmtNum(r.severity, 2)}</span>
          </div>
          <div class="aside">
            <span class="eyebrow">verdict</span>
            <span class="stat-pill ${this._severityHero(r.severity)}">${this._severityLabel(r.severity)}</span>
          </div>
        </div>

        ${r.indicators.length === 0
          ? html`<div class="empty">
              No drift indicators triggered. Predictor running cleanly.
            </div>`
          : html`<ul>
              ${r.indicators.map(
                (ind) => html`
                  <li class=${this._bucketClass(ind.severity)}>
                    <span class="sev">${fmtNum(ind.severity, 2)}</span>
                    <span class="name">${ind.name}</span>
                    <div class="note">${ind.note}</div>
                  </li>
                `
              )}
            </ul>`}
        <p class="muted">
          ml-state.json + drift-history.jsonl · daemon snapshot every 5 min ·
          CLI: <code>coolstep drift</code>
        </p>
      `,
    });
  }
}

customElements.define('drift-tile', DriftTile);
