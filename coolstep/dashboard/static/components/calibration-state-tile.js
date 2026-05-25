import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame } from './_base.js';
import { orchestrator } from './_orchestrator.js';

export class CalibrationStateTile extends LitElement {
  static get priority() { return 'critical'; }
  static styles = [
    tileBaseStyles,
    css`
      ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
      li {
        display: flex; justify-content: space-between; align-items: center;
        padding: 8px 12px;
        font-size: 13px;
        border-radius: var(--radius-sm);
        background: color-mix(in srgb, var(--surface-2) 32%, transparent);
        border: 1px solid var(--border-soft);
      }
      li .gate-name {
        font-family: var(--font-mono);
        color: var(--fg);
      }
      li .gate-val {
        font-family: var(--font-mono);
        font-variant-numeric: tabular-nums;
        color: var(--fg-muted);
        display: inline-flex; align-items: center; gap: var(--sp-2);
      }
      .ready-banner {
        padding: 10px 14px;
        background: var(--ok-soft);
        border: 1px solid color-mix(in srgb, var(--ok) 42%, transparent);
        color: var(--ok);
        border-radius: var(--radius-sm);
        font-size: 13px;
        font-weight: 500;
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
    orchestrator.register('calibration-state-tile', {
      priority: 'critical',
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
    const data = await orchestrator.fetchJson('/api/calibration', null);
    if (data) this.report = data;
    // если fetch упал — сохраняем prior state, не сбрасываем в "0/—"
  }

  render() {
    // Loading skeleton: report=null = first fetch in flight (cold-miss is 16s
    // on 300k+ frame stores). Showing "0/—" during cold fetch read as broken.
    if (this.report === null) {
      return renderFrame({
        title: 'Calibration state',
        meta: null,
        body: html`
          <div class="hero">
            <div class="col">
              <span class="eyebrow cat">gates passed</span>
              <span class="metric cat">…<small>loading</small></span>
            </div>
          </div>
        `,
      });
    }
    const r = this.report;
    const gates = Object.entries(r.gates);
    const passed = gates.filter(([, g]) => g.passed).length;
    const total = gates.length;
    const pct = total ? Math.round((passed / total) * 100) : 0;

    return renderFrame({
      title: 'Calibration state',
      meta: total ? `${passed}/${total}` : null,
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">gates passed</span>
            <span class="metric ${r.ready ? 'ok' : 'cat'}">${passed}<small>/ ${total || '—'}</small></span>
          </div>
          <div class="aside">
            <span class="eyebrow">progress</span>
            <span class="stat-pill ${r.ready ? 'ok' : 'cat'}">${pct}%</span>
          </div>
        </div>

        <div class="meter" role="progressbar"
             aria-valuenow=${passed} aria-valuemin="0" aria-valuemax=${total}>
          <div class="meter-fill" style="width: ${pct}%"></div>
          <div class="meter-label">${pct}% calibrated</div>
        </div>

        <ul>
          ${gates.map(
            ([key, g]) => html`
              <li>
                <span class="gate-name">${key}</span>
                <span class="gate-val">
                  ${fmtNum(g.current, 1)} / ${g.target} ${g.unit}
                  <span class="stat-pill ${g.passed ? 'ok' : 'err'}">
                    ${g.passed ? 'pass' : 'fail'}
                  </span>
                </span>
              </li>
            `
          )}
        </ul>

        ${r.ready
          ? html`<div class="ready-banner">Calibration ready — predictions will surface.</div>`
          : html`<p class="muted">When all gates pass — Predictions tile activates.</p>`}
      `,
    });
  }
}

customElements.define('calibration-state-tile', CalibrationStateTile);
