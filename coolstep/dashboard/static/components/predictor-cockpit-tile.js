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

      /* P2.9.5 — soften per-tick jitter.  Hero metric numbers re-render
         on every 1Hz fetch; without transition they snap and the eye
         catches every change as a flicker.  Color + opacity tween makes
         the value feel like it "settles" rather than ticks.  Tabular
         numerals (set in base styles) keep width stable so transition
         doesn't shift layout. */
      .hero .metric,
      .hero .trend-metric {
        transition: color 80ms var(--ease-out, ease),
                    opacity 80ms var(--ease-out, ease);
      }
      .hero .delta .v {
        transition: color 80ms var(--ease-out, ease);
      }

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
      /* P2.9.2 — signed pill: negative residual = cooling outperforms
         historical envelope.  Tint matches the cockpit's cooling-trail
         colour so eye links pill → canvas trail. */
      /* Cooling-beats-forecast pill (negative residual).  Strong border
         + tinted background so the eye picks it as "good news" amid
         neutral chips.  Explicit hex (not var) — operator reported
         theme variable resolved to red on this host. */
      .acc-chip.cool  {
        border-left-color: #7be0d4 !important;
        background: rgba(123, 224, 212, 0.10);
      }
      .acc-chip.cool .v {
        color: #7be0d4 !important;
      }

      /* P2.9.3 — horizon segmented control.  Lives in the same acc-stack
         flex row as the err/spike pills so all "what is the predictor saying
         right now" lives clustered.  Buttons reset-styled to look like the
         pill chips next door.  (Note: no backticks in this comment — would
         silently close the css template-literal; see G-10 incident.) */
      .hz-toggle {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 2px 6px 2px 10px;
        border-left: 2px solid var(--fg-muted, #9aa);
        background: rgba(255, 255, 255, 0.02);
        font-family: var(--font-mono, monospace);
        font-size: 11px;
      }
      .hz-toggle .hz-lbl {
        color: var(--fg-muted, #9aa);
        text-transform: uppercase;
        letter-spacing: var(--track-pill, 0.08em);
      }
      .hz-seg-group {
        display: inline-flex;
        gap: 0;
        border: 1px solid var(--border-soft, rgba(255,255,255,0.10));
        border-radius: 4px;
        overflow: hidden;
      }
      .hz-seg {
        appearance: none;
        background: transparent;
        border: 0;
        padding: 2px 8px;
        font: inherit;
        color: var(--fg-muted, #9aa);
        cursor: pointer;
        transition: background 160ms var(--ease-out, ease), color 160ms var(--ease-out, ease);
      }
      .hz-seg:hover { color: var(--fg, #cfd); }
      .hz-seg.active {
        background: var(--cat-cooling, #7be0d4);
        color: #061416;
        font-weight: 600;
      }
      .hz-seg + .hz-seg { border-left: 1px solid var(--border-soft, rgba(255,255,255,0.10)); }

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

  /* Available display horizons (s) for the multi-horizon toggle (P2.9.3).
     Math always runs at +30s (KNN training lookahead); UI samples the
     meta-anchored curve at any of these points.  Backend ships forecasts
     dict {h5,h15,h30}; tile picks one to render. */
  static HORIZONS = [5, 15, 30];
  static HORIZON_DEFAULT = 5;
  static HORIZON_STORAGE_KEY = 'coolstep:cockpit:horizon';

  static properties = {
    state: { state: true },
    profileFlipHighlight: { state: true },
    activeHorizon: { state: true },
    // P2.9.5 — display layer.  Hero numbers (live-now t, predicted) tween
    // toward the freshest fetched value via rAF, so a 13.9° → 7.5° flip
    // from a stale → fresh prediction glides instead of teleporting.
    // Underlying state is untouched — this is purely visual smoothing.
    displayT: { state: true },
    displayPred: { state: true },
  };

  constructor() {
    super();
    this.state = null;
    this.profileFlipHighlight = false;
    this._prevProfileChangedAt = null;
    this.activeHorizon = this._loadHorizon();
    this.displayT = null;
    this.displayPred = null;
    this._tweenStart = 0;
    this._tweenFromT = null;
    this._tweenFromPred = null;
    this._tweenToT = null;
    this._tweenToPred = null;
    this._tweenRAF = null;
    this._tweenDurationMs = 400;  // glide window — feels reactive, not laggy
  }

  /* Cubic ease-out for the tween — fast start, soft landing. */
  _ease(t) { const u = 1 - t; return 1 - u * u * u; }

  _kickTween(toT, toPred) {
    // Hold prev value as the "from" reference; if no previous, snap.
    if (this.displayT == null) this.displayT = toT;
    if (this.displayPred == null) this.displayPred = toPred;
    this._tweenFromT = this.displayT;
    this._tweenFromPred = this.displayPred;
    this._tweenToT = toT;
    this._tweenToPred = toPred;
    this._tweenStart = performance.now();
    if (this._tweenRAF == null) {
      const step = () => {
        const elapsed = performance.now() - this._tweenStart;
        const u = Math.min(1, elapsed / this._tweenDurationMs);
        const k = this._ease(u);
        if (this._tweenFromT != null && this._tweenToT != null) {
          this.displayT = this._tweenFromT + (this._tweenToT - this._tweenFromT) * k;
        }
        if (this._tweenFromPred != null && this._tweenToPred != null) {
          this.displayPred = this._tweenFromPred + (this._tweenToPred - this._tweenFromPred) * k;
        }
        if (u < 1) {
          this._tweenRAF = requestAnimationFrame(step);
        } else {
          this._tweenRAF = null;
        }
      };
      this._tweenRAF = requestAnimationFrame(step);
    }
  }

  _loadHorizon() {
    try {
      const v = parseInt(localStorage.getItem(PredictorCockpitTile.HORIZON_STORAGE_KEY) || '', 10);
      if (PredictorCockpitTile.HORIZONS.includes(v)) return v;
    } catch (_) { /* localStorage may be blocked */ }
    return PredictorCockpitTile.HORIZON_DEFAULT;
  }

  _setHorizon(h) {
    if (!PredictorCockpitTile.HORIZONS.includes(h)) return;
    this.activeHorizon = h;
    try { localStorage.setItem(PredictorCockpitTile.HORIZON_STORAGE_KEY, String(h)); }
    catch (_) { /* no-op */ }
    this.updateComplete.then(() => this._draw());
  }

  /* Resolve the predicted temperature for the active horizon — prefers
     server-side forecasts dict; falls back to legacy `predicted` (h=30s)
     so the tile stays sensible if backend is older. */
  _activePredicted() {
    const cur = this.state?.current;
    if (!cur) return null;
    const f = cur.forecasts;
    if (f) {
      const key = `h${this.activeHorizon}`;
      if (f[key] != null) return f[key];
    }
    return cur.predicted;
  }

  /* Horizon → weight in premises, per operator framing 2026-05-13:
     +5s  = full meta responsiveness, predictor stays nimble (meta-led)
     +15s = slight sacrifice of meta, deeper archive trend (balanced)
     +30s = meta almost disabled, ~90% archive (archive-led)
     Math-wise: the saturation curve sampled at +5s shows mostly slope-
     driven projection (close to current + immediate dT/dt); at +30s the
     curve has fully saturated to the KNN-anchored asymptote.  Labels
     describe which layer is dominating what the user sees. */
  _weightLabel() {
    if (this.activeHorizon === 5)  return 'meta-led';
    if (this.activeHorizon === 15) return 'balanced';
    return 'archive-led';
  }

  connectedCallback() {
    super.connectedCallback();
    this._stopped = false;
    this._scheduleRefresh(0);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._stopped = true;
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
    if (this._tweenRAF) { cancelAnimationFrame(this._tweenRAF); this._tweenRAF = null; }
  }

  /* Self-rescheduling timer (replaces setInterval).  setInterval drifts
     because callback execution time accumulates on top of the period
     — operator described "тики раз в секунду, неравномерно".  Chained
     setTimeout fires fetch → on completion measures elapsed → schedules
     next fire to land on the next 1-second grid line.  rAF wraps the
     post-fetch draw so canvas paint aligns to vsync (no half-frame
     stutter when CPU is busy). */
  _scheduleRefresh(delay) {
    if (this._stopped) return;
    this._timer = setTimeout(async () => {
      if (this._stopped) return;
      const startedAt = performance.now();
      // Wrap in try so any single failed fetch doesn't kill the chain —
      // at 10Hz a one-off 502 from FastAPI mid-restart used to break
      // setTimeout chaining ("что-то прерывает", operator 2026-05-13)
      // and the tile stayed frozen until manual browser reload.  Now
      // a failure just skips that frame and the next tick fires normally.
      try {
        await this._refresh();
      } catch (_err) {
        /* swallow — single-poll failures must not break the loop */
      }
      if (this._stopped) return;
      const elapsed = performance.now() - startedAt;
      // 10Hz target (operator request 2026-05-13 — btop-class realtime).
      // Daemon ticks 100ms; ml-state.json atomic-write is sub-ms; backend
      // serves cached chroma stats so /api/predictor-cockpit returns in
      // a few ms.  CSS transitions 80ms (below) so values feel reactive.
      const PERIOD = 100;
      const nextDelay = Math.max(20, PERIOD - elapsed);
      this._scheduleRefresh(nextDelay);
    }, delay);
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
    // P2.9.5: skip canvas repaint when data hasn't materially changed.
    const cur = data?.current || {};
    const trail = data?.actual_trail || [];
    const tail = trail.length ? trail[trail.length - 1] : null;
    const sig = `${cur.ts ?? 0}|${tail?.ts_ago ?? 0}|${tail?.t ?? 0}|${this.activeHorizon}`;
    if (sig !== this._lastDrawSig) {
      this._lastDrawSig = sig;
      this.updateComplete.then(() => this._draw());
    }
    // P2.9.5 — kick rAF tween toward fresh hero values.  Display layer
    // glides instead of jumping when predict-cache flips.
    const toT = (cur.t != null) ? cur.t : this.displayT;
    const toPred = this._activePredicted();
    if (toT != null) this._kickTween(toT, toPred ?? toT);
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

    // Time-series ranges. X: −30s … +Ns (35-60s span). Y: 40°C … 95°C.
    // T_FUT respects the active horizon toggle (P2.9.3) so the canvas
    // visually matches the Δ block — predicted endpoint sits exactly on
    // the right edge of the chart whether user picked +5s, +15s, or +30s.
    const T_PAST = 30;
    const T_FUT  = this.activeHorizon || 5;
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
    // Adaptive X-axis ticks (P2.9.3 polish): past side fixed at −30/−15
    // — gives the eye stable reference points for the actual-trail.
    // Future side scales with active horizon so canvas reads consistently
    // when operator toggles between +5s / +15s / +30s — without this the
    // "now" line wanders from 50% to 86% of canvas width and reads as a
    // leftover stripe from the previous mode.
    const futureTicks = T_FUT >= 30 ? [10, 20, T_FUT]
                      : T_FUT >= 15 ? [5, 10, T_FUT]
                      : [T_FUT];
    for (const t of [-30, -15, 0, ...futureTicks]) {
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
      // Anchor curve on the horizon-active forecast (P2.9.3) so the dashed
      // endpoint sits at the same value as the Δ block reads.
      const activePred = this._activePredicted();
      const Tpred = activePred != null ? activePred : (T0 + cur.slope * tau * F_h);
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
    // P2.9.4 polish — pin lifetime tied to display horizon (operator
     // 2026-05-13: «раньше они исчезали через 15 сек, или через 10 или
     // через 5»).  Earlier hotfix clamped pins to the left edge, which
     // produced an ever-growing fan of red rays anchored at -T_PAST.
     // The right idea: pin's visible age equals the active horizon, then
     // it fades to nothing — same "memory window" as the forecast curve
     // points to, so the cockpit reads consistently.  Quadratic alpha
     // gives a soft tail (perceptually linear).
    const rt = (this.state.residual_trail || []).slice(-8);
    const lifetimeSec = this.activeHorizon;  // 5 / 15 / 30
    ctx.save();
    for (const r of rt) {
      if (r.predicted_at_ago == null) continue;
      const age = r.predicted_at_ago;
      if (age > lifetimeSec) continue;        // expired — disappear
      const t_pred = -age;
      const t_act  = -r.ts_ago;
      if (t_pred < -T_PAST) continue;          // off-screen guard
      // Quadratic fade: α=1 at age=0 (newest), α≈0 at age=lifetime.
      const u = age / lifetimeSec;
      const fade = Math.max(0, (1 - u) * (1 - u));
      const abs = Math.abs(r.residual);
      const c = this._resHex(abs, palette);
      const xP = toX(t_pred);
      const yP = toY(Math.max(Y_MIN, Math.min(Y_MAX, r.predicted)));
      const xA = toX(t_act);
      const yA = toY(Math.max(Y_MIN, Math.min(Y_MAX, r.actual)));
      // Connector — faint thread between "guessed here" and "landed there".
      ctx.strokeStyle = c;
      ctx.globalAlpha = 0.35 * fade;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(xP, yP); ctx.lineTo(xA, yA); ctx.stroke();
      // Hollow ring on the prediction.  Slightly bigger for younger pins
      // so the newest reads as the brightest pin on the canvas.
      const ringR = 2.2 + 1.4 * (1 - u);
      ctx.globalAlpha = 0.90 * fade;
      ctx.beginPath(); ctx.arc(xP, yP, ringR, 0, Math.PI * 2); ctx.stroke();
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

  /* Signed residual → chip class (P2.9.2).  Cooling-beats-forecast semantic:
     consistently-negative residual = predictor over-predicted, system ran
     cooler than expected — это safe direction soft-cooling'а.  Render as
     green («cool» variant): visible margin, not alarm.  Red reserved for
     positive residual (under-prediction — real warning that system went
     hotter than predictor anticipated). */
  _signedResClass(sR) {
    if (sR == null) return 'cold';
    if (Math.abs(sR) < PredictorCockpitTile.RES_OK_C) return 'ok';
    if (sR <= -PredictorCockpitTile.RES_OK_C) return 'cool';
    if (sR > PredictorCockpitTile.RES_WARN_C) return 'err';
    return 'warn';
  }

  /* 30-second signed median — for the cooling-wins pill semantic. */
  _err30sSigned() {
    const trail = this.state?.residual_trail || [];
    const recent = trail
      .filter((r) => r && r.ts_ago != null && r.ts_ago <= 30 && r.residual != null)
      .map((r) => r.residual);
    if (!recent.length) return null;
    recent.sort((a, b) => a - b);
    return recent[Math.floor(recent.length / 2)];
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
    /* Pills use signed median (P2.9.2): consistently-negative residual =
       predictor over-предсказал, cooling выигрывает у historical envelope.
       Это margin coolstep'а, рендерим зелёным как «cooling beats forecast»,
       не red.  Red reserved для positive median residual (under-prediction).
       Falls back to abs from older backend if signed not present. */
    const signed15 = this.state?.median_signed_err_c;
    const signed30 = this._err30sSigned();
    const cls15 = signed15 != null
      ? this._signedResClass(signed15)
      : this._resClass(this.state?.median_abs_err_c);
    const cls30 = signed30 != null
      ? this._signedResClass(signed30)
      : this._resClass(this._err30s());
    const fmtSigned = (v) => {
      if (v == null) return '—';
      const sign = v >= 0 ? '+' : '−';
      return `${sign}${Math.abs(v).toFixed(1)}°`;
    };
    const tip15 = signed15 != null && signed15 < -PredictorCockpitTile.RES_OK_C
      ? `predictor over-predicted by ${Math.abs(signed15).toFixed(1)}°C median over the last 15 min — cooling running ahead of historical envelope (safe direction).`
      : 'median actual − predicted over the last 15 minutes — slow, all workloads pooled';
    const tip30 = signed30 != null && signed30 < -PredictorCockpitTile.RES_OK_C
      ? `predictor over-predicted by ${Math.abs(signed30).toFixed(1)}°C median over the last 30 s — cooling beating live forecast.`
      : 'median actual − predicted over the last 30 seconds — fast, what is happening right now';
    const spike = this.state?.spike || {};
    const spikeChip = spike.active ? this._renderSpikeChip(spike) : null;
    return html`
      <span class="acc-stack">
        ${this._horizonToggle()}
        ${spikeChip}
        <span class="acc-chip ${cls15}" title="${tip15}">
          <span class="lbl">±err · 15m</span>
          <span class="v">${fmtSigned(signed15 ?? this.state?.median_abs_err_c)}</span>
        </span>
        <span class="acc-chip ${cls30}" title="${tip30}">
          <span class="lbl">±err · 30s</span>
          <span class="v">${fmtSigned(signed30 ?? this._err30s())}</span>
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
    const t   = this.displayT   ?? cur.t;
    const predicted = this.displayPred ?? this._activePredicted();
    if (t == null || predicted == null) {
      return html`<div class="delta zero">no forecast</div>`;
    }
    const d = predicted - t;
    const h = this.activeHorizon;
    const cls = Math.abs(d) < 0.2 ? 'zero' : d > 0 ? 'up' : 'down';
    const sign = d >= 0 ? '+' : '';
    return html`
      <div class="delta ${cls}">
        <span class="v">${sign}${d.toFixed(1)}°C</span> in +${fmtNum(h, 0)}s
      </div>
    `;
  }

  /* Segmented control: [ +5s | +15s | +30s ] with `weight: short/balanced/full`
     subtitle.  Sits in the masthead `meta` slot beside the ±err / spike chips
     so all the "what is the predictor currently saying about" controls cluster. */
  _horizonToggle() {
    const items = PredictorCockpitTile.HORIZONS.map((h) => {
      const active = h === this.activeHorizon;
      return html`
        <button
          class="hz-seg ${active ? 'active' : ''}"
          @click=${() => this._setHorizon(h)}
          title="Display forecast horizon — +${h}s"
        >+${h}s</button>
      `;
    });
    return html`
      <span class="hz-toggle" title="Display horizon — math always runs at +30s (KNN lookahead); UI samples the meta-anchored curve at the selected point">
        <span class="hz-lbl">horizon · ${this._weightLabel()}</span>
        <span class="hz-seg-group">${items}</span>
      </span>
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
            <span class="metric cat">${(this.displayT ?? cur.t) != null ? html`${fmtNum(this.displayT ?? cur.t, 1)}<small>°C</small>` : '—'}</span>
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
