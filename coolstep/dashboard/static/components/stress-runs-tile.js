import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame } from './_base.js';
import { orchestrator } from './_orchestrator.js?v=mount-on-register';
import { LangController } from '../i18n/lang-store.js';

export class StressRunsTile extends LitElement {
  static get priority() { return 'normal'; }

  static styles = [
    tileBaseStyles,
    css`
      /* verdict pills */
      .verdict-pill {
        display: inline-flex; align-items: center;
        padding: 1px 7px; border-radius: 999px;
        font-size: 10px; font-weight: 600;
        letter-spacing: 0.06em; text-transform: uppercase;
        font-family: var(--font-mono, monospace);
        background: color-mix(in srgb, var(--fg-dim) 18%, transparent);
        color: var(--fg-muted);
        border: 1px solid color-mix(in srgb, var(--fg-dim) 32%, transparent);
      }
      .verdict-pill.ok {
        background: color-mix(in srgb, var(--ok, #44d88b) 18%, transparent);
        color: var(--ok, #44d88b);
        border-color: color-mix(in srgb, var(--ok, #44d88b) 42%, transparent);
      }
      .verdict-pill.err {
        background: var(--err-soft, rgba(255,115,115,0.14));
        color: var(--err, #ff7373);
        border-color: color-mix(in srgb, var(--err, #ff7373) 42%, transparent);
      }
      .verdict-pill.warn {
        background: var(--warn-soft, rgba(240,196,77,0.14));
        color: var(--warn, #f0c44d);
        border-color: color-mix(in srgb, var(--warn, #f0c44d) 42%, transparent);
      }

      /* armed pill */
      .armed-pill {
        display: inline-flex; align-items: center;
        padding: 1px 7px; border-radius: 999px;
        font-size: 10px; font-weight: 600;
        letter-spacing: 0.06em; text-transform: uppercase;
        font-family: var(--font-mono, monospace);
        background: color-mix(in srgb, var(--cat-color) 18%, transparent);
        color: var(--cat-color);
        border: 1px solid color-mix(in srgb, var(--cat-color) 42%, transparent);
      }
      .armed-pill.dry {
        background: color-mix(in srgb, var(--fg-dim) 18%, transparent);
        color: var(--fg-muted);
        border-color: color-mix(in srgb, var(--fg-dim) 32%, transparent);
      }

      table { width: 100%; font-size: 12px; border-collapse: collapse;
              font-family: var(--font-mono, monospace); }
      th { color: var(--fg-muted); font-weight: 600; text-align: left;
           padding: 6px 8px 6px 0; border-bottom: 1px solid var(--border);
           font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; }
      td { padding: 4px 8px 4px 0; border-bottom: 1px dotted var(--border-soft);
           font-variant-numeric: tabular-nums; }
      td.peak { color: var(--cat-color); font-weight: 500; }
      td.scenario { color: var(--fg); font-weight: 500; }
    `,
  ];

  static properties = {
    runs: { state: true },
    count: { state: true },
  };

  constructor() {
    super();
    this.runs = [];
    this.count = 0;
    this._lang = new LangController(this);
  }

  connectedCallback() {
    super.connectedCallback();
    orchestrator.register('stress-runs-tile', {
      priority: 'normal',
      element: this,
      mountFn: () => this._mount(),
    });
  }

  _mount() {
    this._refresh();
    this._timer = setInterval(() => this._refresh(), 60000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
  }

  async _refresh() {
    const data = await orchestrator.fetchJson('/api/stress-runs', { runs: [], count: 0 });
    this.runs = Array.isArray(data.runs) ? data.runs : [];
    this.count = typeof data.count === 'number' ? data.count : 0;
  }

  _verdictPill(verdict, t) {
    const map = {
      'coolstep_moved_first': { cls: 'ok',   label: t('tile.stressruns.verdict.moved_first') },
      'did_not_move_first':   { cls: 'err',  label: t('tile.stressruns.verdict.did_not') },
      'partial_data':         { cls: 'warn', label: t('tile.stressruns.verdict.partial') },
      'not_observed':         { cls: '',     label: t('tile.stressruns.verdict.not_observed') },
    };
    const v = map[verdict] || { cls: '', label: verdict || '—' };
    return html`<span class="verdict-pill ${v.cls}">${v.label}</span>`;
  }

  _armedPill(armed) {
    if (armed) {
      return html`<span class="armed-pill">armed</span>`;
    }
    return html`<span class="armed-pill dry">dry</span>`;
  }

  _fmtDelta(delta) {
    if (delta == null || delta === undefined) return '—';
    return `${fmtNum(delta, 1)}s`;
  }

  _successCount() {
    return this.runs.filter((r) => r.verdict === 'coolstep_moved_first').length;
  }

  render() {
    const t = (k, v) => this._lang.t(k, v);

    if (this.runs.length === 0) {
      return renderFrame({
        title: t('tile.stressruns.title'),
        meta: 'bench',
        body: html`
          <div class="hero">
            <div class="col">
              <span class="eyebrow">${t('tile.stressruns.eyebrow')}</span>
              <span class="metric">0 / 0</span>
            </div>
          </div>
          <div class="empty">${t('tile.stressruns.empty')}</div>
          <p class="muted">${t('common.source')}: <code>/api/stress-runs</code></p>
        `,
      });
    }

    const success = this._successCount();
    const total = this.runs.length;
    const heroMetric = `${success} / ${total}`;

    return renderFrame({
      title: t('tile.stressruns.title'),
      meta: 'bench',
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">${t('tile.stressruns.eyebrow')}</span>
            <span class="metric cat">${heroMetric}</span>
          </div>
          <div class="aside">
            <span class="eyebrow">${t('common.events')}</span>
            <span class="stat-pill">${total}</span>
          </div>
        </div>

        <table>
          <thead>
            <tr>
              <th>${t('tile.stressruns.col.scenario')}</th>
              <th>${t('tile.stressruns.col.armed')}</th>
              <th>${t('tile.stressruns.col.peak')}</th>
              <th>${t('tile.stressruns.col.above85')}</th>
              <th>${t('tile.stressruns.col.delta')}</th>
              <th>${t('tile.stressruns.col.verdict')}</th>
            </tr>
          </thead>
          <tbody>
            ${this.runs.map((r) => html`
              <tr>
                <td class="scenario">${r.scenario || '—'}</td>
                <td>${this._armedPill(r.armed)}</td>
                <td class="peak">${r.peak_tctl != null ? fmtNum(r.peak_tctl, 1) + '°C' : '—'}</td>
                <td>${r.time_above_85 != null ? fmtNum(r.time_above_85, 0) + 's' : '—'}</td>
                <td>${this._fmtDelta(r.delta_sec)}</td>
                <td>${this._verdictPill(r.verdict, t)}</td>
              </tr>
            `)}
          </tbody>
        </table>

        <p class="muted">${t('common.source')}: <code>/api/stress-runs</code> · 60s poll</p>
      `,
    });
  }
}

customElements.define('stress-runs-tile', StressRunsTile);
