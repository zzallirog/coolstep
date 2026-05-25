import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame } from './_base.js';
import { orchestrator } from './_orchestrator.js';

/** Replaces legacy «Training what+how» card. Reads /api/ml-state. */
export class TrainingTile extends LitElement {
  static get priority() { return 'lazy'; }

  static styles = [
    tileBaseStyles,
    css`
      .features {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 2px 16px;
      }
      .feat {
        display: flex; justify-content: space-between;
        font-size: 12px;
        font-family: var(--font-mono, monospace);
        padding: 4px 0;
        border-bottom: 1px dotted var(--border-soft);
        font-variant-numeric: tabular-nums;
      }
      .feat-name { color: var(--fg-muted); }
      .feat-val { color: var(--fg); font-weight: 500; }
      .banner {
        margin: 12px 0;
        padding: 8px 12px;
        border-radius: 6px;
        font-size: 12px;
        line-height: 1.4;
        border: 1px solid var(--border);
      }
      .banner.warn {
        background: rgba(255, 175, 60, 0.08);
        border-color: rgba(255, 175, 60, 0.35);
        color: var(--warn, #ffb95a);
      }
      .banner.cat {
        background: rgba(120, 180, 255, 0.08);
        border-color: rgba(120, 180, 255, 0.35);
        color: var(--cat-color, #8ab4ff);
      }
      .banner code {
        font-family: var(--font-mono, monospace);
        font-size: 11px;
        background: rgba(0,0,0,0.2);
        padding: 1px 4px;
        border-radius: 3px;
      }
    `,
  ];

  static properties = {
    state: { state: true },
    err: { state: true },
  };

  constructor() {
    super();
    this.state = null;
    this.err = null;
  }

  connectedCallback() {
    super.connectedCallback();
    orchestrator.register('training-tile', {
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
    const data = await orchestrator.fetchJson('/api/ml-state', null);
    if (!data || data.error) {
      // Hold any prior state so a transient blip doesn't blank the tile.
      if (!this.state) {
        this.err = (data && data.error) || 'ml-state not yet written by daemon';
      }
      return;
    }
    this.state = data;
    this.err = null;
  }

  render() {
    if (this.err && !this.state) {
      return renderFrame({
        title: 'Training — what + how',
        body: html`<div class="empty">${this.err}. Daemon writes the snapshot every 30 ticks (~30s).</div>`,
      });
    }
    const s = this.state || {};
    const features = s.features || {};
    const featEntries = Object.entries(features).sort(([a], [b]) => a.localeCompare(b));
    const prob = (s.throttle_prob ?? 0) * 100;
    const probCls = prob >= 70 ? 'err' : prob >= 40 ? 'warn' : 'ok';
    return renderFrame({
      title: 'Training — what + how',
      meta: `tick ${s.tick ?? '—'}`,
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">throttle prob</span>
            <span class="metric ${probCls}">${fmtNum(prob, 1)}<small>%</small></span>
          </div>
          <div class="col">
            <span class="eyebrow">expected peak</span>
            <span class="metric">${fmtNum(s.expected_temp_c, 1)}<small>°C</small></span>
          </div>
          <div class="aside">
            <span class="eyebrow">calibration</span>
            <span class="stat-pill ${s.calibration_ready ? 'ok' : 'warn'}">${s.calibration_ready ? 'ready' : 'pending'}</span>
          </div>
        </div>

        ${s.model_name === 'always_idle_baseline' ? html`
          <div class="banner warn">
            ⚠ Predictor in fallback (<code>always_idle_baseline</code>) —
            ChromaDB unavailable or empty. KNN inference disabled,
            confidence is structural не reflective.
          </div>` : ''}
        ${(s.labeled_count !== undefined && s.labeled_count >= 0 && s.labeled_count < 5) ? html`
          <div class="banner cat">
            warm-up: <strong>${s.labeled_count}</strong> / 5 labelled neighbours.
            KNN held silent until threshold (max(3, top_k/4)).
          </div>` : ''}

        <div class="mini-grid">
          <div class="mini">
            <span class="eyebrow">model</span>
            <span class="v" style="font-size: 0.85rem">${s.model_name || '—'}</span>
          </div>
          <div class="mini">
            <span class="eyebrow">horizon</span>
            <span class="v">${fmtNum(s.horizon_sec, 1)}<small>s</small></span>
          </div>
          <div class="mini">
            <span class="eyebrow">confidence</span>
            <span class="v">${fmtNum(s.confidence * 100, 0)}<small>%</small></span>
          </div>
          <div class="mini ${s.chroma_dir_bytes > 5 * 1024 ** 3 ? 'err' : s.chroma_dir_bytes > 500 * 1024 ** 2 ? 'warn' : ''}">
            <span class="eyebrow">chroma</span>
            <span class="v">${fmtNum(s.chroma_count, 0)}<small>vec · ${_fmtBytes(s.chroma_dir_bytes)}</small></span>
          </div>
          <div class="mini">
            <span class="eyebrow">daemon uptime</span>
            <span class="v">${_fmtDuration(s.uptime_sec)}</span>
          </div>
        </div>

        <div>
          <h3 class="section-h">Predictor inputs · ${featEntries.length}</h3>
          <div class="features">
            ${featEntries.map(
              ([k, v]) => html`
                <div class="feat">
                  <span class="feat-name">${k}</span>
                  <span class="feat-val">${fmtNum(v, 2)}</span>
                </div>
              `
            )}
          </div>
        </div>
        <p class="muted">P0: AlwaysIdleBaseline placeholder. P1 → xgboost on these features.</p>
      `,
    });
  }
}

function _fmtBytes(n) {
  if (n == null || isNaN(n)) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

function _fmtDuration(sec) {
  if (sec == null || isNaN(sec)) return '—';
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  if (sec < 86400) return `${(sec / 3600).toFixed(1)}h`;
  return `${(sec / 86400).toFixed(1)}d`;
}

customElements.define('training-tile', TrainingTile);
