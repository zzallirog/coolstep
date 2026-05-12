import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame } from './_base.js';

/**
 * Predictor cockpit — relational readout of the meta-predictor.
 *
 * Layout (v2: every number is paired with the parameter that gives it
 * meaning):
 *
 *   row 1: NOW  (big)  +Δ in +Ns       │  TREND  →78° in Xs / steady / cooling
 *   row 2: time-series canvas — X=time (past −30s … future +Ns), Y=T
 *           - actual trail (gold)
 *           - forecast curve from now (saturation: T0 + s·τ·(1−e^(−t/τ)))
 *           - σ band shaded around forecast
 *           - past predictions as ghost dots at (predicted_at_ago, predicted),
 *             connected to where they actually landed
 *           - 78°C warn line, 90°C err line, vertical "now" hair
 *   row 3: residual trail tokens (kept from v1)
 *   row 4: bucket meta — profile / correction / σ / bucket-n / log-count
 *
 * Rationale: in v1 every metric was a raw number standing alone — pred ≈ now
 * always (because the 600-frame slope ≈ 0); dT/dt = −0.01°C/s read like a
 * dead instrument; phase-space canvas concentrated all motion on a single
 * Y≈0 line.  v2 expresses each metric *through* another: Δ vs current,
 * time-to-target vs slope, forecast-line laid over actual-line on a shared
 * temperature axis.
 *
 * Endpoint: /api/predictor-cockpit (single aggregated round-trip per second).
 */
export class PredictorCockpitTile extends LitElement {
  static styles = [
    tileBaseStyles,
    css`
      :host { grid-column: span 2; }

      .hero { gap: var(--sp-4, 24px); }

      .delta {
        font-family: var(--font-mono, monospace);
        font-size: 11px;
        letter-spacing: var(--track-pill, 0.08em);
        color: var(--fg-muted);
        margin-top: 4px;
        transition: color 240ms var(--ease-out, ease);
      }
      .delta .v {
        font-variant-numeric: tabular-nums;
        font-weight: 600;
        font-size: 13px;
      }
      .delta.up   { color: var(--warn); }
      .delta.down { color: var(--cat-cooling, #7be0d4); }
      .delta.zero { color: var(--fg-dim); }

      .trend-metric {
        /* slightly smaller than the temperature metric: the trend phrase is
           usually 1-3 short tokens, so we let it wrap before getting tiny */
        font-size: clamp(1.6rem, 5.5cqw, 2.4rem) !important;
        line-height: 1.05;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .trend-metric .unit {
        font-family: var(--font-mono, monospace);
        font-style: normal;
        font-size: 0.45em;
        color: var(--fg-muted);
        margin-left: 6px;
        letter-spacing: var(--track-pill, 0.08em);
        text-transform: uppercase;
        vertical-align: 0.4em;
      }
      .trend-metric.cooling { color: var(--cat-cooling, #7be0d4); }
      .trend-metric.heating { color: var(--warn); }
      .trend-metric.danger  { color: var(--err); }
      .trend-metric.steady  { color: var(--fg-strong); }

      canvas {
        width: 100%;
        height: 240px;
        display: block;
        background: color-mix(in srgb, var(--bg) 70%, transparent);
        border-radius: var(--radius-sm);
        border: 1px solid var(--border-soft);
      }

      /* Twin accuracy chips in the header — 15-minute slow rolling
         signal (predictor quality at scale) and 30-second fast rolling
         signal (response to the workload happening right now).  Sharing
         the same shape means the eye can compare the two numbers
         directly: 15m ±0.5° + 30s ±5° = "we're great in the long run,
         missing under load now". */
      .acc-stack {
        display: inline-flex;
        gap: 6px;
        align-items: stretch;
      }
      .acc-chip {
        display: inline-flex;
        flex-direction: column;
        align-items: flex-start;
        gap: 0;
        padding: 2px 9px;
        border-radius: 2px;
        background: color-mix(in srgb, var(--paper-2, #161c27) 70%, transparent);
        border: 1px solid var(--border, rgba(230,221,196,0.10));
        border-left: 2px solid var(--fg-dim, #5b6678);
        font-family: var(--font-mono, monospace);
        line-height: 1.15;
      }
      .acc-chip .lbl {
        font-size: 9px;
        letter-spacing: var(--track-pill, 0.08em);
        text-transform: uppercase;
        color: var(--fg-muted);
      }
      .acc-chip .v {
        font-size: 12px;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
        color: var(--fg-strong);
      }
      .acc-chip.ok    { border-left-color: var(--ok);   }
      .acc-chip.ok    .v   { color: var(--ok);   }
      .acc-chip.warn  { border-left-color: var(--warn); }
      .acc-chip.warn  .v   { color: var(--warn); }
      .acc-chip.err   { border-left-color: var(--err);  background: var(--err-soft, rgba(255,106,61,0.10)); }
      .acc-chip.err   .v   { color: var(--err);  }
      .acc-chip.cold  { border-left-color: var(--fg-dim); opacity: 0.7; }
      .acc-chip.cold  .v   { color: var(--fg-muted); font-style: italic; }

      /* Spike chip: appears in the same row as the ±err pills when the
         daemon-side detector is active.  Pulses softly so it reads as
         "happening now" rather than a static badge.  Hidden when
         inactive — the cockpit shouldn't carry a "no spike" chip every
         tick.  Hover surfaces the started_at + n_validations detail. */
      .spike-chip {
        display: inline-flex;
        flex-direction: column;
        align-items: flex-start;
        padding: 2px 9px;
        border-radius: 2px;
        background: color-mix(in srgb, var(--err) 12%, transparent);
        border: 1px solid color-mix(in srgb, var(--err) 30%, transparent);
        border-left: 2px solid var(--err);
        font-family: var(--font-mono, monospace);
        line-height: 1.15;
        animation: spike-pulse 1.6s ease-in-out infinite;
      }
      .spike-chip .lbl {
        font-size: 9px;
        letter-spacing: var(--track-pill, 0.08em);
        text-transform: uppercase;
        color: color-mix(in srgb, var(--err) 75%, var(--fg-muted));
      }
      .spike-chip .v {
        font-size: 12px;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
        color: var(--err);
      }
      @keyframes spike-pulse {
        0%, 100% { box-shadow: 0 0 0 0 transparent; }
        50%      { box-shadow: 0 0 0 3px color-mix(in srgb, var(--err) 18%, transparent); }
      }
      @media (prefers-reduced-motion: reduce) {
        .spike-chip { animation: none; }
      }

      /* Legend strip — colour swatches keyed to the canvas marks. Lives
         where the description text used to be; ditches the prose for a
         scannable grid because the same eye that just looked at the
         canvas needs to look up "what was the red ring again?" without
         reading a sentence. */
      .legend {
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        padding: 6px 2px;
        font-family: var(--font-mono, monospace);
        font-size: 10px;
        color: var(--fg-muted);
        letter-spacing: var(--track-pill, 0.08em);
      }
      .legend .item {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        white-space: nowrap;
      }
      .legend .sw {
        width: 22px;
        height: 8px;
        display: inline-block;
        position: relative;
      }
      .legend .sw.gold  { background: var(--warn); }
      .legend .sw.cool  { background: var(--cat-cooling, #7be0d4); }
      .legend .sw.cool.dashed {
        background: linear-gradient(to right,
          var(--cat-cooling, #7be0d4) 0 7px,
          transparent 7px 11px,
          var(--cat-cooling, #7be0d4) 11px 18px,
          transparent 18px 22px);
      }
      .legend .sw.band {
        background: color-mix(in srgb, var(--cat-cooling, #7be0d4) 22%, transparent);
        border-top: 1px dashed color-mix(in srgb, var(--cat-cooling, #7be0d4) 60%, transparent);
        border-bottom: 1px dashed color-mix(in srgb, var(--cat-cooling, #7be0d4) 60%, transparent);
      }
      /* Hollow rings — three colour stops match the residual-trail
         tokens below.  Use circles instead of stripes so the eye links
         them directly to the ghost-dots on the canvas. */
      .legend .ring {
        width: 10px; height: 10px; border-radius: 50%;
        background: transparent;
        border: 1.4px solid var(--fg-dim);
      }
      .legend .ring.ok   { border-color: var(--cat-cooling, #7be0d4); }
      .legend .ring.warn { border-color: var(--warn); }
      .legend .ring.err  { border-color: var(--err); }

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
        transition: background 240ms var(--ease-out, ease);
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

  /* Residual-magnitude colour ladder.  Used by three sites that *must*
     stay in lockstep: the accuracy chip, the residual-trail tokens
     below the canvas, and the ghost rings on the canvas.  If the bands
     diverge between sites the eye-train collapses (a token reads green
     while its ring reads warn for the same residual).  Keep one source. */
  static RES_OK_C = 2.0;
  static RES_WARN_C = 5.0;

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
    this._timer = setInterval(() => this._refresh(), 1000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
  }

  async _refresh() {
    const data = await fetchJson('/api/predictor-cockpit', null);
    if (!data) return;
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

  /* ── Trend phrasing ──────────────────────────────────────────────────
   * "express dT/dt through another parameter" = correlate the slope
   * with the 78°C knee (Arrhenius MTBF threshold) to give the operator
   * an actionable time-to-target, not a raw °C/s number.
   *
   * Branches:
   *  - |slope| < 0.05 °C/s   → steady
   *  - slope < 0             → cooling: "−X°C / Y s" (rate-style framing)
   *  - slope > 0, already ≥ knee → "past 78°C" (urgent)
   *  - slope > 0, equilibrium <= knee → "asymptote ≈ Teq°C" (won't reach)
   *  - slope > 0, will reach → "→78°C in Xs" via saturation inverse:
   *       t = −τ · ln(1 − headroom / (s·τ))
   *    (where T(t) = T0 + s·τ·(1 − e^(−t/τ)) hits the knee at this t)
   * ─────────────────────────────────────────────────────────────────── */
  _trend() {
    const cur = this.state?.current || {};
    const T = cur.t;
    const s = cur.slope;
    if (T == null || s == null) {
      return { label: '—', unit: '', cls: 'steady' };
    }
    if (Math.abs(s) < 0.05) {
      return { label: 'steady', unit: '|dT|<0.05°/s', cls: 'steady' };
    }
    if (s < 0) {
      const drop = Math.min(10, -s * 30);
      const sec  = drop / -s;
      return {
        label: `cooling`,
        unit: `−${drop.toFixed(1)}° / ${sec.toFixed(0)}s`,
        cls: 'cooling',
      };
    }
    const knee = 78.0;
    if (T >= knee) {
      const danger = T >= 85.0;
      return {
        label: danger ? 'past knee' : 'at knee',
        unit: `${T.toFixed(0)}°≥78°`,
        cls: danger ? 'danger' : 'heating',
      };
    }
    const tau = 4.0;
    const maxReach = s * tau;
    const headroom = knee - T;
    if (headroom >= maxReach) {
      const eq = T + maxReach;
      return {
        label: `asymptote`,
        unit: `eq ≈ ${eq.toFixed(0)}°`,
        cls: 'heating',
      };
    }
    const t = -tau * Math.log(1 - headroom / maxReach);
    const cls = t < 8 ? 'danger' : 'heating';
    return {
      label: `→78°`,
      unit: `in ${t.toFixed(0)}s`,
      cls,
    };
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

    const palette = this._palette();
    const { cool, warn, err, dim, grid } = palette;

    // Time-series ranges. X: −30s … +5s (35s span). Y: 40°C … 95°C.
    const T_PAST = 30;
    const horizon = (this.state.current?.horizon_sec) || 5;
    const T_FUT  = Math.max(5, horizon);
    const X_SPAN = T_PAST + T_FUT;
    const Y_MIN = 40, Y_MAX = 95;
    const PAD_L = 36, PAD_R = 8, PAD_T = 8, PAD_B = 18;
    const innerW = w - PAD_L - PAD_R;
    const innerH = h - PAD_T - PAD_B;
    const toX = (t) => PAD_L + ((t + T_PAST) / X_SPAN) * innerW;  // t=−30..+5
    const toY = (T) => PAD_T + (1 - (T - Y_MIN) / (Y_MAX - Y_MIN)) * innerH;

    // Grid + axis labels — temperature ticks every 10°C, time ticks at −30, −15, 0, +5.
    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;
    ctx.fillStyle = dim;
    ctx.font = '10px var(--font-mono, monospace)';
    ctx.textBaseline = 'middle';
    for (let T = 50; T <= 90; T += 10) {
      const y = toY(T);
      ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(w - PAD_R, y); ctx.stroke();
      ctx.textAlign = 'right';
      ctx.fillText(`${T}°`, PAD_L - 4, y);
    }
    ctx.textBaseline = 'top';
    ctx.textAlign = 'center';
    for (const t of [-30, -15, 0, T_FUT]) {
      const x = toX(t);
      ctx.beginPath(); ctx.moveTo(x, PAD_T); ctx.lineTo(x, h - PAD_B); ctx.stroke();
      const lbl = t === 0 ? 'now' : t > 0 ? `+${t}s` : `${t}s`;
      ctx.fillText(lbl, x, h - PAD_B + 3);
    }

    // 78°C knee line and 90°C danger line — dashed.
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = warn;
    ctx.globalAlpha = 0.55;
    const yKnee = toY(78);
    ctx.beginPath(); ctx.moveTo(PAD_L, yKnee); ctx.lineTo(w - PAD_R, yKnee); ctx.stroke();
    ctx.fillStyle = warn;
    ctx.globalAlpha = 0.7;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'bottom';
    ctx.fillText('78°  knee', PAD_L + 4, yKnee - 2);

    ctx.strokeStyle = err;
    ctx.globalAlpha = 0.5;
    const yDanger = toY(90);
    ctx.beginPath(); ctx.moveTo(PAD_L, yDanger); ctx.lineTo(w - PAD_R, yDanger); ctx.stroke();
    ctx.restore();

    // Vertical "now" hair-line, full height, subtle.
    ctx.save();
    ctx.strokeStyle = dim;
    ctx.globalAlpha = 0.4;
    ctx.setLineDash([2, 3]);
    ctx.beginPath(); ctx.moveTo(toX(0), PAD_T); ctx.lineTo(toX(0), h - PAD_B); ctx.stroke();
    ctx.restore();

    // Past trail.  actual_trail entries are { ts_ago, t }.  We want oldest
    // on the left (t_rel = −ts_ago).  Drop anything outside our window.
    const trail = (this.state.actual_trail || [])
      .slice()
      .sort((a, b) => b.ts_ago - a.ts_ago);
    if (trail.length >= 2) {
      ctx.strokeStyle = warn;
      ctx.lineWidth = 1.8;
      ctx.globalAlpha = 0.85;
      ctx.beginPath();
      let first = true;
      for (const r of trail) {
        const t_rel = -r.ts_ago;
        if (t_rel < -T_PAST) continue;
        const x = toX(t_rel);
        const y = toY(Math.max(Y_MIN, Math.min(Y_MAX, r.t)));
        if (first) { ctx.moveTo(x, y); first = false; }
        else       { ctx.lineTo(x, y); }
      }
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    const cur = this.state.current || {};
    if (cur.t != null && cur.slope != null) {
      // Forecast curve anchors on the meta-corrected predicted endpoint
      // (cur.predicted), not on the raw saturation extrapolation —
      // otherwise the dashed line shoots past 90° while the endpoint
      // ring lands at 85° (operator-flagged 2026-05-12: cognitive
      // dissonance between "where the line points" vs "where the model
      // actually predicts").  We keep the saturation *shape* — the
      // (1 − e^(−t/τ)) curve form is still what the chip physically
      // does — and rescale so T(horizon) == cur.predicted.  When meta
      // correction is zero this collapses to the original raw form, so
      // calibrated-bucket behaviour is unchanged.
      const T0 = cur.t;
      const tau = 4.0;
      const horizon = T_FUT;
      // Saturation factor at the horizon — denominator for normalising
      // the rescaled curve so it lands exactly on cur.predicted.
      const F_h = 1 - Math.exp(-horizon / tau);
      // Fallback: if predicted is missing, fall back to raw slope so the
      // curve still has *some* trajectory rather than a flat line.
      const Tpred = cur.predicted != null ? cur.predicted : (T0 + cur.slope * tau * F_h);
      const fwdPoints = [];
      for (let i = 0; i <= 24; i++) {
        const t = (i / 24) * horizon;
        const f = 1 - Math.exp(-t / tau);
        // T(t) = T0 + (Tpred − T0) · f / F_h.  At t=0 → T0.  At
        // t=horizon → T0 + (Tpred−T0) · 1 = Tpred exactly.
        const T = T0 + (Tpred - T0) * (f / F_h);
        fwdPoints.push([t, Math.max(Y_MIN, Math.min(Y_MAX, T))]);
      }
      // σ-corridor: fill a polygon ±σ around the curve.
      const sigma = this.state.median_abs_err_c != null
        ? Math.max(0.5, Math.min(8.0, this.state.median_abs_err_c * 2))
        : 1.0;
      ctx.save();
      ctx.fillStyle = cool;
      ctx.globalAlpha = 0.14;
      ctx.beginPath();
      // top edge: curve + sigma
      for (const [t, T] of fwdPoints) {
        const x = toX(t); const y = toY(Math.min(Y_MAX, T + sigma));
        if (t === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      // bottom edge: curve − sigma, reversed
      for (let i = fwdPoints.length - 1; i >= 0; i--) {
        const [t, T] = fwdPoints[i];
        const x = toX(t); const y = toY(Math.max(Y_MIN, T - sigma));
        ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.fill();
      ctx.restore();

      // Forecast line itself.
      ctx.save();
      ctx.strokeStyle = cool;
      ctx.setLineDash([5, 4]);
      ctx.lineWidth = 1.8;
      ctx.globalAlpha = 0.95;
      ctx.beginPath();
      for (const [t, T] of fwdPoints) {
        const x = toX(t); const y = toY(T);
        if (t === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
      ctx.restore();

      // Predicted endpoint marker.
      if (cur.predicted != null) {
        const xP = toX(T_FUT);
        const yP = toY(Math.max(Y_MIN, Math.min(Y_MAX, cur.predicted)));
        ctx.strokeStyle = cool;
        ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.arc(xP, yP, 4, 0, Math.PI * 2); ctx.stroke();
      }

      // Now dot — solid gold, with halo.
      const xN = toX(0);
      const yN = toY(T0);
      ctx.fillStyle = warn;
      ctx.globalAlpha = 0.35;
      ctx.beginPath(); ctx.arc(xN, yN, 8, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = 1;
      ctx.beginPath(); ctx.arc(xN, yN, 3.5, 0, Math.PI * 2); ctx.fill();
    }

    // Ghost dots: past predictions at (predicted_at_ago, predicted) with
    // a thin segment to where they actually landed.  Coloured by
    // |residual| using the same ok/warn/err thresholds as the token
    // strip below the canvas.
    //
    // Limited to the 8 newest entries (the trail strip below already
    // shows 12 as text) and faded by age — newest at full intensity,
    // oldest at α=0.4 — so the cluster near `now` doesn't read as a
    // single bright blob.  Operator-flagged 2026-05-12: under heavy
    // load the canvas filled with 12+ overlapping rings.
    const rt = (this.state.residual_trail || []).slice(-8);
    ctx.save();
    // Reference age for the fade: use the oldest entry actually drawn.
    const max_age = rt.reduce((m, r) => Math.max(m, r?.predicted_at_ago ?? 0), 1);
    for (const r of rt) {
      if (r.predicted_at_ago == null) continue;
      const t_pred = -r.predicted_at_ago;
      const t_act  = -r.ts_ago;
      if (t_pred < -T_PAST) continue;
      const abs = Math.abs(r.residual);
      const c = this._resHex(abs, palette);
      const xP = toX(t_pred);
      const yP = toY(Math.max(Y_MIN, Math.min(Y_MAX, r.predicted)));
      const xA = toX(t_act);
      const yA = toY(Math.max(Y_MIN, Math.min(Y_MAX, r.actual)));
      // Linear fade by age — α=1.0 at newest, α=0.4 at oldest.
      const age_norm = r.predicted_at_ago / Math.max(1, max_age);
      const fade = 1.0 - 0.6 * age_norm;
      // Connector line: faint, recedes into the background as it ages.
      ctx.strokeStyle = c;
      ctx.globalAlpha = 0.35 * fade;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(xP, yP); ctx.lineTo(xA, yA); ctx.stroke();
      // Hollow ring at prediction.  Slightly larger fade dampening so
      // the newest one stays the brightest pin on the canvas.
      ctx.globalAlpha = 0.85 * fade;
      ctx.beginPath(); ctx.arc(xP, yP, 2.5, 0, Math.PI * 2); ctx.stroke();
    }
    ctx.restore();
  }

  _renderResidualTrail() {
    const trail = this.state?.residual_trail || [];
    if (!trail.length) return html`<div class="muted">awaiting first validation…</div>`;
    return html`
      <div class="residual-trail">
        ${trail.map((r) => {
          const abs = Math.abs(r.residual);
          const cls = this._resClass(abs);
          const sign = r.residual >= 0 ? '+' : '';
          return html`<span class="tok ${cls}" title="actual ${fmtNum(r.actual, 1)}°C, predicted ${fmtNum(r.predicted, 1)}°C">${sign}${r.residual.toFixed(1)}°</span>`;
        })}
      </div>
    `;
  }

  /* Single source for the |residual| → band mapping.  Returns one of
     `ok | warn | err | cold` (cold = no data); callers map the class
     to a CSS chip variant or a canvas hex via `_resHex`. */
  _resClass(absR) {
    if (absR == null) return 'cold';
    if (absR < PredictorCockpitTile.RES_OK_C)   return 'ok';
    if (absR < PredictorCockpitTile.RES_WARN_C) return 'warn';
    return 'err';
  }

  /* Canvas-side colour lookup — takes a palette object built once per
     _draw call and returns the right hex for the given |residual|. */
  _resHex(absR, palette) {
    switch (this._resClass(absR)) {
      case 'ok':   return palette.cool;
      case 'warn': return palette.warn;
      case 'err':  return palette.err;
      default:     return palette.dim;
    }
  }

  /* Resolve the small set of CSS custom properties the canvas needs.
     One getComputedStyle call instead of six; fallback hex values
     match the INSTRUMENT theme so a missing variable still produces
     a coherent palette. */
  _palette() {
    const cs = getComputedStyle(document.documentElement);
    const read = (name, fallback) => (cs.getPropertyValue(name).trim() || fallback);
    return {
      cool: read('--cat-cooling', '#7be0d4'),
      ok:   read('--ok',   '#6fcaa9'),
      warn: read('--warn', '#e6b25c'),
      err:  read('--err',  '#ff6a3d'),
      dim:  read('--fg-dim', '#5b6678'),
      grid: read('--border', 'rgba(255,255,255,0.07)'),
    };
  }

  /* 30-second window: walk residual_trail, take entries with ts_ago
     within 30 s, compute median |residual|.  Returns null on no data
     (cold start or empty trail) — chip then shows "—" in the cold
     style instead of misleading "0°". */
  _err30s() {
    const trail = this.state?.residual_trail || [];
    const recent = trail
      .filter((r) => r && r.ts_ago != null && r.ts_ago <= 30 && r.residual != null)
      .map((r) => Math.abs(r.residual));
    if (!recent.length) return null;
    recent.sort((a, b) => a - b);
    return recent[Math.floor(recent.length / 2)];
  }

  _accuracyBadge() {
    const med15 = this.state?.median_abs_err_c;
    const med30 = this._err30s();
    const cls15 = this._resClass(med15);
    const cls30 = this._resClass(med30);
    const spike = this.state?.spike || {};
    // The spike chip rides the same row as the err pills when the
    // daemon-side detector is active.  Inactive → render nothing (the
    // cockpit shouldn't carry a "no spike" chip 99% of the time).
    const spikeChip = spike.active ? this._renderSpikeChip(spike) : null;
    return html`
      <span class="acc-stack">
        ${spikeChip}
        <span class="acc-chip ${cls15}" title="median |actual − predicted| over the last 15 minutes — slow, all workloads pooled">
          <span class="lbl">±err · 15m</span>
          <span class="v">${med15 != null ? `${med15.toFixed(1)}°` : '—'}</span>
        </span>
        <span class="acc-chip ${cls30}" title="median |actual − predicted| over the last 30 seconds — fast, what's happening right now">
          <span class="lbl">±err · 30s</span>
          <span class="v">${med30 != null ? `${med30.toFixed(1)}°` : '—'}</span>
        </span>
      </span>
    `;
  }

  _renderSpikeChip(spike) {
    const dur = spike.duration_s != null ? Math.max(0, spike.duration_s) : 0;
    const workload = spike.workload_label || '?';
    const maxRes = spike.max_abs_residual;
    const detail = maxRes != null ? `±${maxRes.toFixed(1)}°` : '';
    return html`
      <span class="spike-chip"
            title="Spike active: predictor was surprised by ≥${spike.thresholds?.entry_c ?? 5}°C residual for ${spike.thresholds?.entry_n ?? 2} consecutive validations · max|res| ${maxRes != null ? maxRes.toFixed(1) + '°' : '—'} · workload ${workload} · n=${spike.n_validations ?? 0}">
        <span class="lbl">⚡ spike · ${workload}</span>
        <span class="v">${dur.toFixed(0)}s ${detail}</span>
      </span>
    `;
  }

  _renderLegend() {
    return html`
      <div class="legend">
        <span class="item"><span class="sw gold"></span>actual past 30s</span>
        <span class="item"><span class="sw cool dashed"></span>forecast +5s</span>
        <span class="item"><span class="sw band"></span>σ ±err·2</span>
        <span class="item"><span class="ring ok"></span>past pred ≤ 2°</span>
        <span class="item"><span class="ring warn"></span>2 – 5°</span>
        <span class="item"><span class="ring err"></span>miss &gt; 5°</span>
      </div>
    `;
  }

  /* Delta block under the NOW metric: predicted − now in absolute °C,
     framed by horizon ("in +5s") and tinted by direction.  Direction
     colour matches the trail/forecast colours on the canvas so eye
     tracks line→number. */
  _deltaPredict() {
    const cur = this.state?.current || {};
    if (cur.t == null || cur.predicted == null) {
      return html`<div class="delta zero">no forecast</div>`;
    }
    const d = cur.predicted - cur.t;
    const h = cur.horizon_sec ?? 5;
    const cls = Math.abs(d) < 0.2 ? 'zero' : d > 0 ? 'up' : 'down';
    const sign = d >= 0 ? '+' : '';
    return html`
      <div class="delta ${cls}">
        <span class="v">${sign}${d.toFixed(1)}°C</span> in +${fmtNum(h, 0)}s
      </div>
    `;
  }

  render() {
    const cur = this.state?.current || {};
    const profile = this.state?.active_tuned_profile;
    const buckets = this.state?.meta_buckets;
    const logCount = this.state?.residual_log_count;
    const reason = cur.reason || '';
    const corrMatch = reason.match(/meta-correction\s+([+-]?\d+(?:\.\d+)?)/);
    const nMatch    = reason.match(/n=(\d+)/);
    const sigmaMatch = reason.match(/σ=([\d.]+)/);
    const trend = this._trend();
    const slopeStr = cur.slope != null
      ? `${cur.slope >= 0 ? '+' : ''}${cur.slope.toFixed(2)}°C/s`
      : '—';

    return renderFrame({
      title: 'Predictor cockpit',
      meta: this._accuracyBadge(),
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">live now</span>
            <span class="metric cat">${cur.t != null ? html`${fmtNum(cur.t, 1)}<small>°C</small>` : '—'}</span>
            ${this._deltaPredict()}
          </div>
          <div class="col">
            <span class="eyebrow">trend</span>
            <span class="metric trend-metric ${trend.cls}">
              ${trend.label}<span class="unit">${trend.unit}</span>
            </span>
            <div class="delta zero">dT/dt ${slopeStr}</div>
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

        ${this._renderLegend()}
      `,
    });
  }
}

customElements.define('predictor-cockpit-tile', PredictorCockpitTile);
