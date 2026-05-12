import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame } from './_base.js';

/**
 * Predictor cockpit — phase-space view of the meta-predictor.
 *
 * Layout:
 *   row 1: live T  |  predicted +Ns (with σ band)  |  accuracy 24h badge
 *   row 2: phase-space canvas — X=T, Y=dT/dt, trajectory tail + forward arrow
 *   row 3: residual trail strip — last 12 validations as inline tokens
 *   row 4: bucket + meta diagnostics (correction °C, σ°C, n samples, profile)
 *
 * What is NOT here (intentionally):
 *  - no 800ms validation pulse animation
 *  - no countdown ticker
 *  - no efficiency-histogram chrome
 *
 * Coordinate system: phase space (T, dT/dt).  Newton-cooling makes clean
 * arcs on this plot: the trajectory curves toward (T_eq, 0).  Predictor
 * working == arrow lands where we then observe ourselves.
 *
 * Endpoint: /api/predictor-cockpit (single aggregated round-trip per second).
 */
export class PredictorCockpitTile extends LitElement {
  static styles = [
    tileBaseStyles,
    css`
      :host { grid-column: span 2; }

      canvas {
        width: 100%;
        height: 220px;
        display: block;
        background: color-mix(in srgb, var(--bg) 70%, transparent);
        border-radius: var(--radius-sm);
        border: 1px solid var(--border-soft);
      }

      .accuracy-badge {
        display: inline-flex;
        align-items: baseline;
        gap: 4px;
        padding: 3px 10px;
        border-radius: 999px;
        background: color-mix(in srgb, var(--surface-2) 50%, transparent);
        border: 1px solid var(--border);
        font-family: var(--font-mono, monospace);
        font-size: 11px;
        color: var(--fg-muted);
      }
      .accuracy-badge .pct {
        color: var(--ok);
        font-weight: 600;
        font-size: 13px;
      }
      .accuracy-badge.warn .pct { color: var(--warn); }
      .accuracy-badge.err .pct  { color: var(--err); }

      .residual-trail {
        display: flex;
        gap: 4px;
        flex-wrap: wrap;
        font-family: var(--font-mono, monospace);
        font-size: 10.5px;
        padding: 6px 2px;
      }
      .residual-trail .tok {
        padding: 2px 6px;
        border-radius: 3px;
        background: color-mix(in srgb, var(--surface-2) 40%, transparent);
        border: 1px solid var(--border-soft);
        font-variant-numeric: tabular-nums;
      }
      .residual-trail .tok.ok   { color: var(--ok); }
      .residual-trail .tok.warn { color: var(--warn); }
      .residual-trail .tok.err  { color: var(--err); }

      .bucket-strip {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        font-family: var(--font-mono, monospace);
        font-size: 10.5px;
        color: var(--fg-muted);
        padding-top: 4px;
        border-top: 1px dotted var(--border-soft);
        transition: background 1.8s ease-out;
      }
      .bucket-strip.flip {
        background: color-mix(in srgb, var(--cat-cooling) 18%, transparent);
        border-radius: 3px;
        padding: 4px 6px;
      }
      .bucket-strip strong { color: var(--fg); font-weight: 600; }
    `,
  ];

  static properties = {
    state: { state: true },
    profileFlipHighlight: { state: true },
  };

  constructor() {
    super();
    this.state = null;
    this.profileFlipHighlight = false;
    this._prevProfileChangedAt = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    // 1 Hz refresh — matches collector tick rate; cheap server endpoint.
    this._timer = setInterval(() => this._refresh(), 1000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
  }

  async _refresh() {
    const data = await fetchJson('/api/predictor-cockpit', null);
    if (!data) return;
    // Detect profile flip: profile_changed_at changed from last poll → flash.
    const newChangedAt = data.profile_changed_at;
    if (
      this._prevProfileChangedAt !== null
      && newChangedAt !== null
      && newChangedAt !== this._prevProfileChangedAt
    ) {
      this.profileFlipHighlight = true;
      setTimeout(() => { this.profileFlipHighlight = false; }, 2000);
    }
    this._prevProfileChangedAt = newChangedAt;
    this.state = data;
    this.updateComplete.then(() => this._draw());
  }

  _draw() {
    const cnv = this.renderRoot?.querySelector('canvas');
    if (!cnv || !this.state) return;
    const dpr = window.devicePixelRatio || 1;
    const w = cnv.clientWidth;
    const h = cnv.clientHeight;
    cnv.width = w * dpr;
    cnv.height = h * dpr;
    const ctx = cnv.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    const cs = getComputedStyle(document.documentElement);
    const cool = cs.getPropertyValue('--cat-cooling').trim() || '#6aa5ff';
    const ok   = cs.getPropertyValue('--ok').trim() || '#44d88b';
    const warn = cs.getPropertyValue('--warn').trim() || '#f0c44d';
    const err  = cs.getPropertyValue('--err').trim() || '#e75566';
    const dim  = cs.getPropertyValue('--fg-dim').trim() || '#58627c';
    const grid = cs.getPropertyValue('--border').trim() || 'rgba(255,255,255,0.07)';

    // Phase-space ranges.
    const T_MIN = 30, T_MAX = 95;
    const DT_MIN = -3.0, DT_MAX = 3.0;
    const toX = (T) => ((T - T_MIN) / (T_MAX - T_MIN)) * w;
    const toY = (dT) => h - ((dT - DT_MIN) / (DT_MAX - DT_MIN)) * h;

    // Grid: T every 10°C, dT/dt every 1°C/s.
    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;
    ctx.fillStyle = dim;
    ctx.font = '10px var(--font-mono, monospace)';
    for (let T = 40; T <= 90; T += 10) {
      const x = toX(T);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      ctx.fillText(`${T}°`, x + 3, h - 4);
    }
    for (let dT = DT_MIN + 1; dT < DT_MAX; dT += 1) {
      const y = toY(dT);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
      if (dT === 0) {
        ctx.fillStyle = dim;
        ctx.fillText('steady', 4, y - 3);
      } else {
        ctx.fillStyle = dim;
        ctx.fillText(`${dT > 0 ? '+' : ''}${dT.toFixed(0)}`, 4, y - 3);
      }
    }

    // Past trajectory: walk actual_trail backwards, infer dT/dt from
    // consecutive frames.  The trail is ts_ago ascending → we want
    // chronological order: newest is ts_ago=0, oldest highest ts_ago.
    const trail = (this.state.actual_trail || []).slice().sort((a, b) => b.ts_ago - a.ts_ago);
    if (trail.length >= 2) {
      ctx.strokeStyle = cool;
      ctx.globalAlpha = 0.45;
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      let first = true;
      for (let i = 1; i < trail.length; i++) {
        const T = trail[i].t;
        const T_prev = trail[i - 1].t;
        const dt_age = trail[i - 1].ts_ago - trail[i].ts_ago;
        if (dt_age <= 0) continue;
        const slope = (T - T_prev) / dt_age;
        const x = toX(T);
        const y = toY(Math.max(DT_MIN, Math.min(DT_MAX, slope)));
        if (first) { ctx.moveTo(x, y); first = false; }
        else       { ctx.lineTo(x, y); }
      }
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    // Current point + arrow to predicted.
    const cur = this.state.current || {};
    if (cur.t !== undefined && cur.slope !== undefined) {
      const xCur = toX(cur.t);
      const yCur = toY(Math.max(DT_MIN, Math.min(DT_MAX, cur.slope)));

      // σ band: an ellipse around the predicted point, width=σ°C, height=σ°C/s.
      if (cur.predicted != null && this.state.median_abs_err_c != null) {
        const sigma_T = Math.max(1.0, this.state.median_abs_err_c * 2);
        const xPred = toX(cur.predicted);
        // The forward arrow assumes slope decays toward 0 over horizon — pure visual.
        // Predicted slope ≈ (predicted - current) / horizon.
        const predSlope = (cur.predicted - cur.t) / (cur.horizon_sec || 5);
        const yPred = toY(Math.max(DT_MIN, Math.min(DT_MAX, predSlope)));

        // σ ellipse around predicted.
        ctx.strokeStyle = cool;
        ctx.globalAlpha = 0.35;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.ellipse(
          xPred, yPred,
          (sigma_T / (T_MAX - T_MIN)) * w,
          ((sigma_T / 2) / (DT_MAX - DT_MIN)) * h,
          0, 0, Math.PI * 2,
        );
        ctx.stroke();
        ctx.globalAlpha = 1;

        // Arrow from current to predicted.
        ctx.strokeStyle = cool;
        ctx.lineWidth = 1.8;
        ctx.beginPath();
        ctx.moveTo(xCur, yCur);
        ctx.lineTo(xPred, yPred);
        ctx.stroke();
        // Arrowhead.
        const ang = Math.atan2(yPred - yCur, xPred - xCur);
        ctx.fillStyle = cool;
        ctx.beginPath();
        ctx.moveTo(xPred, yPred);
        ctx.lineTo(xPred - 7 * Math.cos(ang - 0.4), yPred - 7 * Math.sin(ang - 0.4));
        ctx.lineTo(xPred - 7 * Math.cos(ang + 0.4), yPred - 7 * Math.sin(ang + 0.4));
        ctx.closePath();
        ctx.fill();
      }

      // Live now — solid gold dot.
      ctx.fillStyle = warn;
      ctx.globalAlpha = 0.35;
      ctx.beginPath(); ctx.arc(xCur, yCur, 9, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = 1;
      ctx.fillStyle = warn;
      ctx.beginPath(); ctx.arc(xCur, yCur, 4.5, 0, Math.PI * 2); ctx.fill();
    }
  }

  _renderResidualTrail() {
    const trail = this.state?.residual_trail || [];
    if (!trail.length) return html`<div class="muted">awaiting first validation…</div>`;
    return html`
      <div class="residual-trail">
        ${trail.map((r) => {
          const abs = Math.abs(r.residual);
          const cls = abs < 2 ? 'ok' : abs < 5 ? 'warn' : 'err';
          const sign = r.residual >= 0 ? '+' : '';
          return html`<span class="tok ${cls}" title="actual ${fmtNum(r.actual, 1)}°C, predicted ${fmtNum(r.predicted, 1)}°C">${sign}${r.residual.toFixed(1)}°</span>`;
        })}
      </div>
    `;
  }

  _accuracyBadge() {
    const pct = this.state?.accuracy_pct;
    const med = this.state?.median_abs_err_c;
    if (pct == null) return html`<span class="accuracy-badge"><span>warming up</span></span>`;
    const cls = pct >= 0.85 ? '' : pct >= 0.65 ? 'warn' : 'err';
    return html`
      <span class="accuracy-badge ${cls}" title="median |residual| over last 15 min">
        accuracy 15m
        <span class="pct">${Math.round(pct * 100)}%</span>
        <span style="color: var(--fg-dim)">(±${fmtNum(med, 1)}°)</span>
      </span>
    `;
  }

  render() {
    const cur = this.state?.current || {};
    const profile = this.state?.active_tuned_profile;
    const buckets = this.state?.meta_buckets;
    const logCount = this.state?.residual_log_count;
    // Bucket meta: parse the reason string for n/correction (carried in
    // Prediction.reason by MetaPredictor).  Cheap regex, falls back to '—'.
    const reason = cur.reason || '';
    const corrMatch = reason.match(/meta-correction\s+([+-]?\d+(?:\.\d+)?)/);
    const nMatch    = reason.match(/n=(\d+)/);
    const sigmaMatch = reason.match(/σ=([\d.]+)/);

    return renderFrame({
      title: 'Predictor cockpit',
      meta: this._accuracyBadge(),
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">live now</span>
            <span class="metric cat">${cur.t != null ? html`${fmtNum(cur.t, 1)}<small>°C</small>` : '—'}</span>
          </div>
          <div class="col">
            <span class="eyebrow">predicted +${fmtNum(cur.horizon_sec ?? 5, 0)}s</span>
            <span class="metric" style="color: var(--cat-cooling)">${cur.predicted != null ? html`${fmtNum(cur.predicted, 1)}<small>°C</small>` : '—'}</span>
          </div>
          <div class="col">
            <span class="eyebrow">dT/dt</span>
            <span class="metric">${cur.slope != null ? html`${(cur.slope >= 0 ? '+' : '') + fmtNum(cur.slope, 2)}<small>°C/s</small>` : '—'}</span>
          </div>
        </div>

        <canvas></canvas>

        <h3 class="section-h">Residual trail · last 12 validations</h3>
        ${this._renderResidualTrail()}

        <div class="bucket-strip ${this.profileFlipHighlight ? 'flip' : ''}">
          <span>profile <strong>${profile || '—'}</strong></span>
          ${corrMatch ? html`<span>correction <strong>${corrMatch[1]}°C</strong></span>` : ''}
          ${sigmaMatch ? html`<span>σ <strong>${sigmaMatch[1]}°C</strong></span>` : ''}
          ${nMatch ? html`<span>bucket n=<strong>${nMatch[1]}</strong></span>` : ''}
          <span>buckets <strong>${buckets ?? 0}</strong></span>
          <span>log <strong>${logCount ?? 0}</strong></span>
        </div>

        <p class="muted">
          Phase-space: X=T<sub>cpu</sub>, Y=dT/dt. Trail = past 30s, arrow = predicted at horizon.
          σ-ellipse from rolling residual std. Predicted hits actual ↔ trail catches the arrow tip.
        </p>
      `,
    });
  }
}

customElements.define('predictor-cockpit-tile', PredictorCockpitTile);
