import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame } from './_base.js';
import { orchestrator } from './_orchestrator.js';
import { LangController } from '../i18n/lang-store.js';

/** Predictor breakdown — splits the final throttle_prob into its two
 * contributing signals (KNN vote + trajectory overlay).
 *
 * Backend `/api/predictor-breakdown` re-computes the trajectory probability
 * from features so the tile never has to parse the reason string. KNN
 * contribution is inferred as max(0, final - trajectory). This is an
 * upper bound on KNN — the merge in predictor.py uses max(), so when
 * trajectory dominates KNN's real probability can be lower than the
 * displayed bar. That's acceptable for an at-a-glance attribution view
 * (the reason string below carries the precise narrative).
 *
 * Polling 5 s — same cadence as neighbours-tile to track latest ml-state.json. */
export class PredictorBreakdownTile extends LitElement {
  static get priority() { return 'lazy'; }

  static styles = [
    tileBaseStyles,
    css`
      .bars {
        display: flex;
        flex-direction: column;
        gap: var(--sp-2, 8px);
      }
      .bar-row {
        display: grid;
        grid-template-columns: 88px 1fr 56px;
        align-items: center;
        gap: var(--sp-2, 8px);
        font-size: 12px;
        font-family: var(--font-mono, monospace);
      }
      .bar-label {
        color: var(--fg-muted);
        text-transform: uppercase;
        font-size: 10px;
        letter-spacing: 0.08em;
        font-weight: 600;
      }
      .bar-track {
        position: relative;
        height: 14px;
        border-radius: 999px;
        background: color-mix(in srgb, var(--bg, #05070f) 60%, transparent);
        box-shadow: inset 0 0 0 1px var(--border, rgba(255,255,255,0.07));
        overflow: hidden;
      }
      .bar-fill {
        position: absolute;
        inset: 0 auto 0 0;
        border-radius: inherit;
        transition: width 240ms var(--ease-out, ease);
      }
      .bar-fill.knn {
        background: linear-gradient(90deg,
          color-mix(in srgb, var(--cat-graph, #bc8cff) 60%, transparent),
          color-mix(in srgb, var(--cat-graph, #bc8cff) 95%, white));
      }
      .bar-fill.traj {
        background: linear-gradient(90deg,
          color-mix(in srgb, var(--warn, #f0c44d) 60%, transparent),
          color-mix(in srgb, var(--warn, #f0c44d) 95%, white));
      }
      .bar-val {
        color: var(--fg);
        font-variant-numeric: tabular-nums;
        text-align: right;
        font-weight: 600;
      }
      .reason-box {
        padding: 10px 14px;
        background: color-mix(in srgb, var(--cat-color) 8%, transparent);
        border-left: 3px solid var(--cat-color);
        border-radius: var(--radius-sm);
        font-size: 12px;
        font-family: var(--font-mono, monospace);
        color: var(--fg);
        word-break: break-word;
      }
      .baseline-banner {
        padding: 10px 14px;
        background: var(--warn-soft, rgba(240,196,77,0.14));
        border: 1px solid color-mix(in srgb, var(--warn) 42%, transparent);
        color: var(--warn);
        border-radius: var(--radius-sm);
        font-size: 12px;
      }
      .model-pill {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 2px 10px;
        height: 20px;
        border-radius: 999px;
        font-family: var(--font-mono);
        font-size: 11px;
      }
      .model-pill.knn {
        background: color-mix(in srgb, var(--cat-graph, #bc8cff) 14%, transparent);
        color: var(--cat-graph, #bc8cff);
        border: 1px solid color-mix(in srgb, var(--cat-graph, #bc8cff) 42%, transparent);
      }
      .model-pill.cold {
        background: var(--warn-soft);
        color: var(--warn);
        border: 1px solid color-mix(in srgb, var(--warn) 42%, transparent);
      }
    `,
  ];

  static properties = {
    state: { state: true },
    error: { state: true },
  };

  constructor() {
    super();
    this.state = null;
    this.error = null;
    this._lang = new LangController(this);
  }

  connectedCallback() {
    super.connectedCallback();
    orchestrator.register('predictor-breakdown-tile', {
      priority: 'lazy',
      element: this,
      mountFn: () => this._mount(),
    });
  }

  _mount() {
    this._refresh();
    this._timer = setInterval(() => this._refresh(), 5000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
  }

  async _refresh() {
    try {
      const r = await orchestrator.fetch('/api/predictor-breakdown', { cache: 'no-store' });
      if (r.status === 404) {
        this.state = null;
        this.error = 'no-ml-state';
        return;
      }
      if (!r.ok) {
        this.error = `http-${r.status}`;
        return;
      }
      this.state = await r.json();
      this.error = null;
    } catch (_e) {
      this.error = 'fetch-failed';
    }
  }

  _probCls(p) {
    if (p >= 0.7) return 'err';
    if (p >= 0.4) return 'warn';
    return 'ok';
  }

  render() {
    const t = (k, v) => this._lang.t(k, v);

    if (this.error === 'no-ml-state' || this.state == null) {
      return renderFrame({
        title: t('tile.predictor.title'),
        body: html`
          <div class="empty">
            ${t('tile.predictor.empty')}
          </div>
        `,
      });
    }

    const s = this.state;
    const finalProb = Number(s.throttle_prob || 0);
    const trajProb = Number(s.trajectory_prob_estimate || 0);
    // KNN contribution is inferred as max(0, final - traj). Upper bound:
    // when trajectory wins the merge, KNN's real prob can be lower. We
    // surface the residual so the bars always sum to <=final.
    const knnContribution = Math.max(0, finalProb - trajProb);
    const conf = Number(s.confidence || 0);
    const isBaseline = s.model_name === 'always_idle_baseline';

    const finalPct = finalProb * 100;
    const knnPct = knnContribution * 100;
    const trajPct = trajProb * 100;

    return renderFrame({
      title: t('tile.predictor.title'),
      meta: s.model_name
        ? html`<span class="model-pill ${isBaseline ? 'cold' : 'knn'}">${s.model_name}</span>`
        : null,
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">${t('tile.predictor.final')}</span>
            <span class="metric ${this._probCls(finalProb)}">${fmtNum(finalPct, 0)}<small>%</small></span>
          </div>
          <div class="col">
            <span class="eyebrow">${t('tile.predictor.confidence')}</span>
            <span class="metric">${fmtNum(conf * 100, 0)}<small>%</small></span>
          </div>
          <div class="col">
            <span class="eyebrow">${t('tile.predictor.expected_temp')}</span>
            <span class="metric">${s.expected_temp_c != null
              ? html`${fmtNum(s.expected_temp_c, 1)}<small>°C</small>`
              : '—'}</span>
          </div>
        </div>

        ${isBaseline
          ? html`<div class="baseline-banner">${t('tile.predictor.model.cold')}</div>`
          : ''}

        <div class="bars" role="group" aria-label="${t('tile.predictor.title')}">
          <div class="bar-row">
            <span class="bar-label">${t('tile.predictor.knn')}</span>
            <div class="bar-track" role="progressbar"
                 aria-valuenow="${knnPct.toFixed(0)}" aria-valuemin="0" aria-valuemax="100">
              <div class="bar-fill knn" style="width: ${knnPct.toFixed(1)}%"></div>
            </div>
            <span class="bar-val">${fmtNum(knnPct, 0)}<small>%</small></span>
          </div>
          <div class="bar-row">
            <span class="bar-label">${t('tile.predictor.trajectory')}</span>
            <div class="bar-track" role="progressbar"
                 aria-valuenow="${trajPct.toFixed(0)}" aria-valuemin="0" aria-valuemax="100">
              <div class="bar-fill traj" style="width: ${trajPct.toFixed(1)}%"></div>
            </div>
            <span class="bar-val">${fmtNum(trajPct, 0)}<small>%</small></span>
          </div>
        </div>

        ${s.reason
          ? html`
            <div>
              <span class="eyebrow">${t('tile.predictor.reason')}</span>
              <div class="reason-box">${s.reason}</div>
            </div>`
          : ''}

        <p class="muted">
          T<sub>cpu</sub>=${fmtNum(s.features?.cpu_temp_max, 1)}°C ·
          slope=${fmtNum(s.features?.cpu_temp_slope_per_sec, 2)}°C/s ·
          load=${fmtNum(s.features?.cpu_load_max, 0)}%
          · refresh 5s
        </p>
      `,
    });
  }
}

customElements.define('predictor-breakdown-tile', PredictorBreakdownTile);
