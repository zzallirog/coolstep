import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame } from './_base.js';
import { orchestrator } from './_orchestrator.js?v=mount-on-register';

/**
 * Predictor cockpit — relational readout of the meta-predictor.
 *
 * Layout (v2: every number is paired with the parameter that gives it
 * meaning):
 *
 *   row 1: NOW  (big)  +Δ in +Ns       │  TREND  →78° in Xs / steady / cooling
 *   row 2: time-series canvas — X=time (past scope+horizon … future +Ns), Y=T
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

      /* Masthead truncation fix (2026-05-13 — operator: «верхняя шапка
         странно выглядит на разных масштабах»).  The frame header uses
         a 3-column grid (FIG, title, meta) — meta now bundles horizon
         toggle + pin-count toggle + spike chip + 2 acc chips, which
         shoves col 3 wide enough that the italic serif title gets
         ellipsis-truncated to "Predict...".  Override at host: when
         the tile is narrower than ~860px (container query), let the
         header wrap so title gets its own row above the meta block.
         At ≥ 860px the original 3-col grid is preserved. */
      header { row-gap: 6px; column-gap: var(--sp-3, 16px); }
      header h2 {
        white-space: normal !important;  /* let title breathe instead of … */
        text-overflow: clip !important;
      }
      @container (max-width: 860px) {
        header {
          grid-template-columns: auto 1fr;
        }
        header .meta {
          grid-column: 1 / -1;
          justify-self: stretch;
          align-self: flex-start;
        }
      }
      .acc-stack {
        flex-wrap: wrap;
        row-gap: 4px;
      }
      /* Compact mode (2026-05-13 — operator: «ошибки спрыгивают вниз при
         120%»).  At narrow container widths (zoomed-in or short screens)
         the ±err chips' uppercase labels eat enough room to force a wrap
         onto a second row.  Drop the labels and tighten padding so the
         numeric value stands alone — the colour band and tooltip still
         carry the meaning. */
      @container (max-width: 760px) {
        .acc-chip { padding: 2px 6px; }
        .acc-chip .lbl { display: none; }
        .acc-chip .v { font-size: 11px; }
        .hz-toggle { padding: 2px 4px 2px 8px; }
        .hz-toggle .hz-lbl { display: none; }
        .hz-seg { padding: 2px 6px; font-size: 10px; }
        .spike-chip .lbl { font-size: 8px; }
        .spike-chip { padding: 2px 6px; }
      }

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
      .residual-trail .tok.cool { color: var(--cat-cooling, #7be0d4); }
      .residual-trail .tok.warn { color: var(--warn); }
      .residual-trail .tok.err  { color: var(--err); }
      .residual-trail .tok.quiet {
        opacity: 0.58;
      }
      .residual-trail .tok.margin {
        opacity: 0.86;
      }
      .residual-trail .tok.controlled {
        border-style: dashed;
        opacity: 0.74;
      }

      .reading-map {
        margin-top: 9px;
        border: 1px solid var(--border-soft);
        border-radius: 6px;
        background:
          linear-gradient(135deg, color-mix(in srgb, var(--cat-cooling, #7be0d4) 7%, transparent), transparent 42%),
          color-mix(in srgb, var(--surface-2) 42%, transparent);
        overflow: hidden;
        transition: background 1.8s ease-out, border-color 1.8s ease-out;
      }
      .reading-map.flip {
        background: color-mix(in srgb, var(--cat-cooling) 18%, transparent);
        border-color: color-mix(in srgb, var(--cat-cooling) 40%, var(--border-soft));
      }
      .map-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
        flex-wrap: wrap;
        padding: 7px 10px;
        border-bottom: 1px solid var(--border-soft);
        background: linear-gradient(90deg, color-mix(in srgb, var(--bg) 34%, transparent), transparent);
      }
      .map-kicker {
        font-family: var(--font-mono, monospace);
        font-size: 10px;
        letter-spacing: var(--track-pill, 0.08em);
        text-transform: uppercase;
        color: var(--cat-cooling, #7be0d4);
      }
      .map-sub {
        font-family: var(--font-mono, monospace);
        font-size: 10px;
        letter-spacing: var(--track-pill, 0.08em);
        text-transform: uppercase;
        color: var(--fg-muted);
      }
      .map-glyph-strip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        margin-left: auto;
      }
      .map-glyph-strip .glyph {
        width: 20px;
        height: 16px;
        opacity: 0.88;
      }
      .map-grid {
        display: grid;
        grid-template-columns: minmax(0, 1.08fr) minmax(330px, 0.92fr);
        gap: 8px;
        padding: 8px;
      }
      .trail-band,
      .rune-board,
      .ledger-board {
        min-width: 0;
        border: 1px solid color-mix(in srgb, var(--border-soft) 82%, transparent);
        background: color-mix(in srgb, var(--bg) 44%, transparent);
        border-radius: 5px;
      }
      .trail-band {
        grid-column: 1 / -1;
        display: grid;
        grid-template-columns: auto minmax(0, 1fr);
        gap: 9px;
        align-items: center;
        padding: 7px 8px;
      }
      .rune-board,
      .ledger-board {
        padding: 8px;
      }
      .lane-head {
        display: flex;
        align-items: center;
        gap: 6px;
        min-height: 16px;
        font-family: var(--font-mono, monospace);
        text-transform: uppercase;
        letter-spacing: var(--track-pill, 0.08em);
        font-size: 10px;
        color: var(--fg-muted);
      }
      .lane-head strong {
        color: var(--fg-strong);
        font-weight: 600;
      }
      .lane-head small {
        color: var(--fg-dim);
        letter-spacing: 0.04em;
      }
      .glyph {
        width: 24px;
        height: 18px;
        flex: 0 0 auto;
        color: var(--fg-muted);
        overflow: visible;
      }
      .glyph path,
      .glyph circle,
      .glyph rect,
      .glyph line {
        fill: none;
        stroke: currentColor;
        stroke-width: 1.8;
        stroke-linecap: round;
        stroke-linejoin: round;
      }
      .glyph rect.fill {
        fill: currentColor;
        stroke: none;
        opacity: 0.22;
      }
      .glyph.gold { color: var(--warn); }
      .glyph.oath { color: var(--cat-cooling, #7be0d4); }
      .glyph.veil { color: color-mix(in srgb, var(--cat-cooling, #7be0d4) 80%, var(--fg-muted)); }
      .glyph.law { color: var(--fg-dim); }
      .glyph.scar { color: var(--err); }
      .glyph.margin { color: var(--cat-cooling, #7be0d4); }
      .glyph.bound { color: var(--fg-muted); }
      .glyph.bound circle,
      .glyph.oath path,
      .glyph.law path {
        stroke-dasharray: 3 3;
      }
      .rune-list {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 6px;
        margin-top: 6px;
      }
      .rune {
        display: grid;
        grid-template-columns: 24px minmax(0, 1fr);
        gap: 6px;
        align-items: center;
        min-height: 34px;
        padding: 5px 6px;
        border-radius: 4px;
        background: color-mix(in srgb, var(--surface-2) 34%, transparent);
        border: 1px solid color-mix(in srgb, var(--border-soft) 70%, transparent);
      }
      .rune b {
        display: block;
        color: var(--fg);
        font-size: 10.5px;
        line-height: 1.15;
      }
      .rune span:last-child {
        display: block;
        color: var(--fg-muted);
        font-size: 9px;
        line-height: 1.15;
      }
      .trail-band .residual-trail {
        padding: 0;
      }
      .trail-note {
        margin-top: 5px;
        color: var(--fg-muted);
        font-family: var(--font-mono, monospace);
        font-size: 10px;
        line-height: 1.35;
      }
      .ledger-grid {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 6px;
        margin-top: 6px;
      }
      .ledger-cell {
        min-width: 0;
        border: 1px solid color-mix(in srgb, var(--border-soft) 74%, transparent);
        background: color-mix(in srgb, var(--surface-2) 26%, transparent);
        border-radius: 4px;
        padding: 5px 6px;
      }
      .ledger-cell .k {
        display: block;
        color: var(--fg-muted);
        font-family: var(--font-mono, monospace);
        font-size: 9px;
        letter-spacing: var(--track-pill, 0.08em);
        text-transform: uppercase;
      }
      .ledger-cell .v {
        display: block;
        color: var(--fg-strong);
        font-family: var(--font-mono, monospace);
        font-size: 12px;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .ledger-cell.cool .v { color: var(--cat-cooling, #7be0d4); }
      .ledger-cell.warn .v { color: var(--warn); }
      .ledger-cell.err .v { color: var(--err); }
      .ledger-foot {
        display: flex;
        gap: 9px;
        flex-wrap: wrap;
        margin-top: 6px;
        color: var(--fg-muted);
        font-family: var(--font-mono, monospace);
        font-size: 9.5px;
        letter-spacing: 0.03em;
      }
      .watch-pulse {
        margin-top: 7px;
        display: flex;
        gap: 9px;
        flex-wrap: wrap;
        font-family: var(--font-mono, monospace);
        font-size: 10px;
        color: var(--fg-muted);
        opacity: 0.78;
      }
      .watch-pulse strong {
        color: var(--fg);
        font-weight: 600;
      }
      .watch-pulse.stale {
        color: var(--warn);
        opacity: 1;
      }
      .watch-pulse.stale strong { color: var(--warn); }
      .watch-pulse .inflight {
        color: var(--cat-cooling, #7be0d4);
        animation: refresh-pulse 1.4s ease-in-out infinite;
      }
      .trust-prior     { color: var(--fg-muted); opacity: 0.55; }
      .trust-shrunk    { color: var(--cat-warming, #d8a36b); }
      .trust-confident { color: var(--cat-cooling, #7be0d4); }
      @keyframes refresh-pulse {
        50% { opacity: 0.45; }
      }
      @container (max-width: 620px) {
        .map-grid { grid-template-columns: 1fr; }
        .trail-band { grid-template-columns: 1fr; }
        .rune-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .ledger-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      }
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

  /* Operator 2026-05-13: 12 pins was too many — visual noise.  Keep
     selectable 3/5/10 so user can dial signal-vs-context.  10 max. */
  static PIN_COUNTS = [3, 5, 10];
  static PIN_COUNT_DEFAULT = 5;
  static PIN_COUNT_STORAGE_KEY = 'coolstep:cockpit:pinCount';
  static PIN_LABEL_MAX = 2;
  // Canvas-only de-noise: residual_trail keeps exact records, but rings
  // closer than this visual bucket collapse to the worst miss. Otherwise a
  // single recovery edge paints several near-identical vertical bars.
  static PIN_CLUSTER_MIN_GAP_S = 4.0;

  /* Past-window scope (seconds) — operator 2026-05-13: «пины по сей день
     за -30 сек, а должны казаться актуальными. с возможностью расширения
     скоупа».  30s default keeps recent past close; 60/120 lets the
     operator look further back without rebinning manually.  Canvas
     X-axis spans [-scope, +T_FUT]. */
  static SCOPES = [30, 60, 120];
  static SCOPE_DEFAULT = 30;
  static SCOPE_STORAGE_KEY = 'coolstep:cockpit:scope';

  static properties = {
    state: { state: true },
    profileFlipHighlight: { state: true },
    activeHorizon: { state: true },
    activePinCount: { state: true },
    activeScope: { state: true },
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
    this.activePinCount = this._loadPinCount();
    this.activeScope = this._loadScope();
    this.displayT = null;
    this.displayPred = null;
    this._tweenStart = 0;
    this._tweenFromT = null;
    this._tweenFromPred = null;
    this._tweenToT = null;
    this._tweenToPred = null;
    this._tweenRAF = null;
    this._tweenDurationMs = 400;  // glide window — feels reactive, not laggy
    // Predicted-now extrapolation. When the daemon stalls (CPU-quota
    // throttle, store rotate, deep _backfill_labels), ml-state.json
    // doesn't refresh for 1-3 s. Without this, the hero numerals freeze
    // on screen and the operator sees "тикает, потом думает". With it,
    // displayT keeps moving along the last known slope vector — so the
    // dial breathes even when the underlying data is paused. Δ clamped
    // to ±2 °C so a noisy slope can't drift the dial into fantasy land.
    this._extrapBaseT = null;
    this._extrapBaseSlope = 0;
    this._extrapBaseAt = 0;
    this._extrapRAF = null;
    this._extrapMaxDelta = 2.0;
    this._onVisibilityChange = () => {
      if (document.hidden || this._stopped) return;
      if (this._timer) { clearTimeout(this._timer); this._timer = null; }
      this._scheduleRefresh(0);
    };
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
          // Tween landed — capture extrapolation base from the just-
          // settled values + the current slope. Extrapolation loop picks
          // it up next frame.
          this._extrapBaseT = this._tweenToT;
          this._extrapBaseSlope = Number(this.state?.current?.slope) || 0;
          this._extrapBaseAt = performance.now();
        }
      };
      this._tweenRAF = requestAnimationFrame(step);
    }
  }

  /* Linear forward extrapolation of displayT along the last observed
     slope vector. Runs continuously after the tile mounts; pauses while
     a tween is animating (tween wins for the 400 ms glide window).
     Δ clamped to ±_extrapMaxDelta so a stale, noisy slope can't push
     the dial off the chart. */
  _startExtrapolation() {
    if (this._extrapRAF != null) return;
    const step = () => {
      if (this._stopped) { this._extrapRAF = null; return; }
      if (this._tweenRAF == null && this._extrapBaseT != null) {
        const dt_s = (performance.now() - this._extrapBaseAt) / 1000;
        const raw = this._extrapBaseSlope * dt_s;
        const clamped = Math.max(-this._extrapMaxDelta,
                                 Math.min(this._extrapMaxDelta, raw));
        this.displayT = this._extrapBaseT + clamped;
      }
      this._extrapRAF = requestAnimationFrame(step);
    };
    this._extrapRAF = requestAnimationFrame(step);
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

  _loadPinCount() {
    try {
      const v = parseInt(localStorage.getItem(PredictorCockpitTile.PIN_COUNT_STORAGE_KEY) || '', 10);
      if (PredictorCockpitTile.PIN_COUNTS.includes(v)) return v;
    } catch (_) { /* localStorage may be blocked */ }
    return PredictorCockpitTile.PIN_COUNT_DEFAULT;
  }

  _setPinCount(n) {
    if (!PredictorCockpitTile.PIN_COUNTS.includes(n)) return;
    this.activePinCount = n;
    try { localStorage.setItem(PredictorCockpitTile.PIN_COUNT_STORAGE_KEY, String(n)); }
    catch (_) { /* no-op */ }
    this.updateComplete.then(() => this._draw());
  }

  _loadScope() {
    try {
      const v = parseInt(localStorage.getItem(PredictorCockpitTile.SCOPE_STORAGE_KEY) || '', 10);
      if (PredictorCockpitTile.SCOPES.includes(v)) return v;
    } catch (_) { /* localStorage may be blocked */ }
    return PredictorCockpitTile.SCOPE_DEFAULT;
  }

  _setScope(s) {
    if (!PredictorCockpitTile.SCOPES.includes(s)) return;
    this.activeScope = s;
    try { localStorage.setItem(PredictorCockpitTile.SCOPE_STORAGE_KEY, String(s)); }
    catch (_) { /* no-op */ }
    // Scope change → refetch + redraw (server bins residuals by scope).
    this._lastDrawSig = null;
    this._refresh().then(() => this._draw());
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

  static get priority() { return 'normal'; }

  connectedCallback() {
    super.connectedCallback();
    orchestrator.register('predictor-cockpit-tile', {
      priority: 'normal',
      element: this,
      mountFn: () => this._mount(),
    });
  }

  _mount() {
    this._stopped = false;
    document.addEventListener('visibilitychange', this._onVisibilityChange);
    this._scheduleRefresh(0);
    this._startExtrapolation();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._stopped = true;
    document.removeEventListener('visibilitychange', this._onVisibilityChange);
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
    if (this._tweenRAF) { cancelAnimationFrame(this._tweenRAF); this._tweenRAF = null; }
    if (this._extrapRAF) { cancelAnimationFrame(this._extrapRAF); this._extrapRAF = null; }
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
      const nextDelay = Math.max(50, this._pollPeriodMs() - elapsed);
      this._scheduleRefresh(nextDelay);
    }, delay);
  }

  _pollPeriodMs() {
    if (document.hidden) return 5000;
    const cur = this.state?.current || {};
    const pred = this._activePredicted();
    const nearKnee = (cur.t != null && cur.t >= 76)
      || (pred != null && pred >= 78)
      || this.state?.spike?.active === true;
    const fastSlope = Math.abs(Number(cur.slope) || 0) >= 0.8;
    return nearKnee || fastSlope ? 1000 : 2000;
  }

  async _refresh() {
    const scope = this.activeScope || PredictorCockpitTile.SCOPE_DEFAULT;
    const data = await orchestrator.fetchJson(`/api/predictor-cockpit?scope_s=${scope}`, null);
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
    // Draw on fetch — normal polling means at most one repaint every 2 s,
    // which is cheap and preserves the v0.5.4 visual model the operator
    // confirmed worked ("раньше он не виснул", 2026-05-13). Earlier rAF
    // pump + wall-clock drift left a visible blank between the trail
    // tail and the "now" line as drift accumulated — reverted.
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

    // Time-series ranges. X: −T_PAST … +Ns. Y: 40°C … 95°C.
    // T_FUT respects the active horizon toggle (P2.9.3) so the canvas
    // visually matches the Δ block — predicted endpoint sits exactly on
    // the right edge of the chart whether user picked +5s, +15s, or +30s.
    const horizonSec = this.state?.current?.horizon_sec || 30;
    // Residual validations are selected by scope, but the prediction point
    // was made one horizon earlier. Keep scope+horizon visible so the ring
    // (forecast origin) and landing dot can both live on the same canvas.
    const scopeSec = this.activeScope || PredictorCockpitTile.SCOPE_DEFAULT;
    const T_PAST = Math.max(scopeSec + horizonSec, horizonSec + 8);
    const T_FUT  = this.activeHorizon || 5;
    const X_SPAN = T_PAST + T_FUT;
    const Y_MIN = 40, Y_MAX = 95;
    const PAD_L = 36, PAD_R = 8, PAD_T = 8, PAD_B = 18;
    const innerW = w - PAD_L - PAD_R;
    const innerH = h - PAD_T - PAD_B;
    const toX = (t) => PAD_L + ((t + T_PAST) / X_SPAN) * innerW;
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
    // Past ticks adapt to scope: 3 evenly-spaced marks across the window
    // so the X-axis stays readable at scope=30, 60, 120 without crowding.
    const pastTickStep = Math.max(10, Math.round(T_PAST / 3 / 5) * 5);
    const pastTicks = [];
    for (let t = -pastTickStep; t >= -T_PAST + 1; t -= pastTickStep) {
      pastTicks.push(t);
    }
    for (const t of [...pastTicks, 0, ...futureTicks]) {
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
    // 2-point EMA smoothing on the drawn polyline.  Raw samples at 10Hz
    // jitter visibly when the chip oscillates ±0.5°C between consecutive
    // sysfs reads.  α=0.4 is a soft pull (more weight on the new sample,
    // small carry from prior) — keeps the line responsive to a real load
    // jump while suppressing single-sample noise. We mutate copies of the
    // points, not the original state, so the numeric residual trail below
    // (which the operator reads as ground truth) is untouched.
    for (let i = 1; i < trail.length; i++) {
      const cur = trail[i], prev = trail[i - 1];
      if (cur.t == null || prev.t == null || isNaN(cur.t) || isNaN(prev.t)) continue;
      trail[i] = { ...cur, t: 0.4 * cur.t + 0.6 * prev.t };
    }
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
      // Anchor the saturation curve on the *tweened* current temperature
      // (displayT) when available so the envelope's left endpoint, the
      // trail's tail-extension, and the gold "now" dot all sit at the
      // same pixel between fetches. Falls back to the raw cur.t value
      // before the first tween lands.
      const T0 = this.displayT ?? cur.t;
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

      // Raw inertia overlay: current short-slope only, without learned
      // archive/meta correction. This is not a "without coolstep" counterfactual;
      // it only answers whether KNN agrees with the immediate dT/dt direction.
      const slope_raw = Number(cur.slope) || 0;
      const slope_clamped = Math.max(-3, Math.min(3, slope_raw));
      const physPoints = [];
      for (let i = 0; i <= 24; i++) {
        const t = (i / 24) * horizon;
        const f = 1 - Math.exp(-t / tau);
        const T = T0 + slope_clamped * tau * f;
        physPoints.push([t, Math.max(Y_MIN, Math.min(Y_MAX, T))]);
      }
      ctx.save();
      ctx.strokeStyle = dim;
      ctx.setLineDash([2, 3]);
      ctx.lineWidth = 1.2;
      ctx.globalAlpha = 0.65;
      ctx.beginPath();
      for (const [t, T] of physPoints) {
        const x = toX(t); const y = toY(T);
        if (t === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
      ctx.restore();

      // Predicted endpoint marker — pinned to the active-horizon forecast
      // value so the ring sits at the end of the teal dashed curve, not at
      // the legacy `cur.predicted` (h30) endpoint which floated 25°C above
      // the visible line when h5 was the active tab (2026-05-13 fix).
      const endpointT = activePred != null ? activePred : cur.predicted;
      if (endpointT != null) {
        const xP = toX(T_FUT);
        const yP = toY(Math.max(Y_MIN, Math.min(Y_MAX, endpointT)));
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

    // Past-prediction marks: ring = when the forecast was made; landing dot =
    // where the actual validation arrived. This keeps every pin spatially
    // anchored to its own prediction time instead of repainting it near now.
    // Selection is policy-driven, not just latest N: hot misses and cool
    // margins each get one possible label, nearby validations collapse to
    // one representative, and controlled pins are quieter.
    const maxPins = this.activePinCount || PredictorCockpitTile.PIN_COUNT_DEFAULT;
    const rt = this._canvasResidualPins(maxPins, T_PAST);
    if (rt.length > 0 && cur.t != null) {
      ctx.save();
      const max_age = rt.reduce(
        (m, r) => Math.max(m, this._pinAnchorAgo(r, horizonSec)), 1);
      // Canvas-bg colour for the inner halo — punches a hole through
      // the gold trail so small-residual pins stay legible.
      const bgColor = '#0a0d12';
      // A prediction has two times: when it was made and when it landed.
      // Draw both; otherwise pins appear to teleport around the chart.
      for (const r of rt) {
        if (r.ts_ago == null) continue;
        const predAgo = this._pinAnchorAgo(r, horizonSec);
        const tPred = -predAgo;
        const tAct = -r.ts_ago;
        if (tPred < -T_PAST || tAct < -T_PAST) continue;
        const cls = this._signedResClass(r.residual);
        const c = this._signedResHex(r.residual, palette);
        const xPred = toX(tPred);
        const xAct = toX(tAct);
        const yPred = toY(Math.max(Y_MIN, Math.min(Y_MAX, r.predicted)));
        const yAct  = toY(Math.max(Y_MIN, Math.min(Y_MAX, r.actual)));
        const age_norm = predAgo / Math.max(1, max_age);
        const fade = Math.max(0.28, 1.0 - 0.68 * age_norm);
        const isLabelPin = r.label_pin === true;
        const isHotMiss = r.residual > PredictorCockpitTile.RES_WARN_C;
        const importance = isHotMiss ? 1.0
          : cls === 'warn' ? 0.78
          : cls === 'cool' ? 0.56
          : 0.42;
        const controlFade = r.intervened ? 0.72 : 1.0;
        const radius = isLabelPin ? 5.8 : 4.4;
        // Bridge: forecast origin → actual landing. Direction carries
        // both time and residual: rising bridge = actual hotter; falling =
        // actual cooler than the oath.
        ctx.setLineDash(r.intervened ? [3, 3] : []);
        ctx.strokeStyle = c;
        ctx.globalAlpha = (isLabelPin ? 0.42 : 0.22) * fade * importance * controlFade;
        ctx.lineWidth = isLabelPin ? 1.4 : 1.0;
        ctx.beginPath(); ctx.moveTo(xPred, yPred); ctx.lineTo(xAct, yAct); ctx.stroke();
        // Landing dot on the gold trail at validation time.
        ctx.fillStyle = c;
        ctx.globalAlpha = (isLabelPin ? 0.7 : 0.36) * fade * importance * controlFade;
        ctx.beginPath(); ctx.arc(xAct, yAct, isLabelPin ? 1.6 : 1.15, 0, Math.PI * 2); ctx.fill();
        // Halo around the ring — punches a hole in the trail so the
        // ring's residual-band colour stays legible for small residuals.
        ctx.globalAlpha = (isLabelPin ? 0.82 : 0.56) * fade * controlFade;
        ctx.fillStyle = bgColor;
        ctx.beginPath(); ctx.arc(xPred, yPred, radius, 0, Math.PI * 2); ctx.fill();
        // Outer ring at forecast origin — the model's oath.
        ctx.globalAlpha = (isLabelPin ? 0.92 : 0.56) * fade * importance * controlFade;
        ctx.strokeStyle = c;
        ctx.lineWidth = isLabelPin ? 2.2 : 1.4;
        ctx.beginPath(); ctx.arc(xPred, yPred, radius, 0, Math.PI * 2); ctx.stroke();
        ctx.setLineDash([]);
        // Numeric residual label next to each ring.  Sign convention:
        //   residual > 0 → actual was hotter than predicted (under-pred);
        //                  pin sits below trail; label above pin.
        //   residual < 0 → actual was cooler than predicted (over-pred);
        //                  pin sits above trail; label below pin.
        if (isLabelPin) {
          ctx.globalAlpha = 0.9 * fade * controlFade;
          ctx.fillStyle = c;
          ctx.font = 'bold 9.5px var(--font-mono, monospace)';
          ctx.textAlign = 'center';
          ctx.textBaseline = r.residual >= 0 ? 'bottom' : 'top';
          const labelDy = r.residual >= 0 ? -8 : 8;
          const sign = r.residual >= 0 ? '+' : '−';
          const labelTxt = `${sign}${Math.abs(r.residual).toFixed(1)}°`;
          ctx.fillText(labelTxt, xPred, yPred + labelDy);
        }
      }
      ctx.restore();
    }
  }

  _canvasResidualPins(maxPins, tPast) {
    const horizonSec = this.state?.current?.horizon_sec || 30;
    const trail = (this.state?.residual_trail || [])
      .filter((r) => (
        r && r.ts_ago != null && r.residual != null
        && r.ts_ago >= 0 && r.ts_ago <= tPast
        && this._pinAnchorAgo(r, horizonSec) <= tPast
      ));
    const passive = trail.filter((r) => r.intervened !== true);
    const source = (passive.length ? passive : trail)
      .slice()
      .sort((a, b) => this._pinAnchorAgo(a, horizonSec) - this._pinAnchorAgo(b, horizonSec));
    const scope = this.activeScope || PredictorCockpitTile.SCOPE_DEFAULT;
    const gap = Math.max(
      PredictorCockpitTile.PIN_CLUSTER_MIN_GAP_S,
      Math.min(7.0, scope / 12),
    );
    const clusters = [];
    for (const r of source) {
      const anchorAgo = this._pinAnchorAgo(r, horizonSec);
      const existing = clusters.find((c) => (
        Math.abs(this._pinAnchorAgo(c, horizonSec) - anchorAgo) <= gap
      ));
      if (existing == null) {
        clusters.push({ ...r, cluster_count: 1 });
        continue;
      }
      existing.cluster_count = (existing.cluster_count || 1) + 1;
      if (this._pinVisualScore(r, tPast) > this._pinVisualScore(existing, tPast)) {
        Object.assign(existing, r, { cluster_count: existing.cluster_count });
      }
    }
    const selected = this._spatialPinSelection(clusters, maxPins, tPast, horizonSec);
    const labelSet = this._pinLabelSet(selected, tPast);
    return selected
      .sort((a, b) => this._pinAnchorAgo(b, horizonSec) - this._pinAnchorAgo(a, horizonSec))
      .map((r) => ({ ...r, label_pin: labelSet.has(r) }));
  }

  _pinAnchorAgo(r, horizonSec = 30) {
    if (r?.predicted_at_ago != null) return Number(r.predicted_at_ago);
    return (Number(r?.ts_ago) || 0) + horizonSec;
  }

  _spatialPinSelection(clusters, maxPins, tPast, horizonSec) {
    if (clusters.length <= maxPins) return clusters.slice();
    const segments = Array.from({ length: maxPins }, () => []);
    for (const r of clusters) {
      const anchor = this._pinAnchorAgo(r, horizonSec);
      const idx = Math.max(0, Math.min(maxPins - 1, Math.floor((anchor / Math.max(1, tPast)) * maxPins)));
      segments[idx].push(r);
    }
    const picked = [];
    for (const segment of segments) {
      if (!segment.length) continue;
      picked.push(segment.sort((a, b) => this._pinVisualScore(b, tPast) - this._pinVisualScore(a, tPast))[0]);
    }
    if (picked.length >= maxPins) return picked.slice(0, maxPins);
    const pickedSet = new Set(picked);
    const fill = clusters
      .filter((r) => !pickedSet.has(r))
      .sort((a, b) => this._pinVisualScore(b, tPast) - this._pinVisualScore(a, tPast))
      .slice(0, maxPins - picked.length);
    return [...picked, ...fill];
  }

  _pinLabelSet(selected, tPast) {
    const labelSet = new Set();
    const bestHot = selected
      .filter((r) => r.residual >= PredictorCockpitTile.RES_WARN_C)
      .sort((a, b) => this._pinVisualScore(b, tPast) - this._pinVisualScore(a, tPast))[0];
    const bestCool = selected
      .filter((r) => r.residual <= -PredictorCockpitTile.RES_WARN_C)
      .sort((a, b) => this._pinVisualScore(b, tPast) - this._pinVisualScore(a, tPast))[0];
    if (bestHot) labelSet.add(bestHot);
    if (bestCool) labelSet.add(bestCool);
    if (labelSet.size >= PredictorCockpitTile.PIN_LABEL_MAX) return labelSet;
    selected
      .filter((r) => !labelSet.has(r) && Math.abs(r.residual) >= PredictorCockpitTile.RES_WARN_C)
      .sort((a, b) => this._pinVisualScore(b, tPast) - this._pinVisualScore(a, tPast))
      .slice(0, PredictorCockpitTile.PIN_LABEL_MAX - labelSet.size)
      .forEach((r) => labelSet.add(r));
    return labelSet;
  }

  _pinVisualScore(r, tPast) {
    const residual = Number(r?.residual) || 0;
    const abs = Math.abs(residual);
    const hotMiss = residual > 0 ? 2.4 : 0.9;
    const controlled = r?.intervened === true ? 0.45 : 1.0;
    const age = Math.max(0, Math.min(1, this._pinAnchorAgo(r) / Math.max(1, tPast)));
    const recency = 1.15 - age * 0.25;
    const cluster = 1 + Math.min(0.35, Math.max(0, (r?.cluster_count || 1) - 1) * 0.08);
    return abs * hotMiss * controlled * recency * cluster;
  }

  _renderResidualTrail() {
    const trail = this.state?.residual_trail || [];
    if (!trail.length) return html`<div class="muted">awaiting first validation…</div>`;
    return html`
      <div class="residual-trail">
        ${trail.map((r) => {
          const abs = Math.abs(r.residual);
          const cls = this._signedResClass(r.residual);
          const controlled = r.intervened === true;
          const quiet = abs < PredictorCockpitTile.RES_WARN_C || r.residual < 0;
          const sign = r.residual >= 0 ? '+' : '';
          const verbs = (r.intervention_verbs || []).join(', ');
          const title = controlled
            ? `controlled residual (${verbs || 'actuator overlap'}): actual ${fmtNum(r.actual, 1)}°C, predicted ${fmtNum(r.predicted, 1)}°C`
            : `passive residual: actual ${fmtNum(r.actual, 1)}°C, predicted ${fmtNum(r.predicted, 1)}°C`;
          return html`<span class="tok ${cls} ${quiet ? 'quiet' : ''} ${controlled ? 'controlled' : ''}" title="${title}">${sign}${r.residual.toFixed(1)}°</span>`;
        })}
      </div>
    `;
  }

  /* Magnitude-only residual ladder.  Signed render paths use
     `_signedResClass` so cool margins do not read as hot misses. */
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
  _recentResiduals30() {
    const trail = this.state?.residual_trail || [];
    const recent = trail
      .filter((r) => r && r.ts_ago != null && r.ts_ago <= 30 && r.residual != null);
    const passive = recent.filter((r) => r.intervened !== true);
    return passive.length ? passive : recent;
  }

  _err30sSigned() {
    const recent = this._recentResiduals30().map((r) => r.residual);
    if (!recent.length) return null;
    recent.sort((a, b) => a - b);
    return recent[Math.floor(recent.length / 2)];
  }

  _signedResHex(residual, palette) {
    switch (this._signedResClass(residual)) {
      case 'ok':   return palette.ok;
      case 'cool': return palette.cool;
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
    const recent = this._recentResiduals30().map((r) => Math.abs(r.residual));
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
    const degradedChip = this.state?.degraded ? html`
      <span class="acc-chip warn"
            title="KNN not in the loop (${this.state?.model_name || 'fallback'}): forecast is physics+meta only, confidence is structural, not reflective. Workload recognition disabled.">
        <span class="lbl">⛔ fallback</span>
        <span class="v">${(this.state?.model_name || '?').replace('+meta', '')}</span>
      </span>` : null;
    return html`
      <span class="acc-stack">
        ${this._horizonToggle()}
        ${this._scopeToggle()}
        ${this._pinCountToggle()}
        ${degradedChip}
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

  _renderWatchPulse() {
    const s = this.state || {};
    const age = s.prediction_age_sec;
    const skipped = s.predict_refresh_skipped ?? 0;
    const refreshMs = s.predict_refresh_last_ms ?? 0;
    const inflight = s.predict_refresh_inflight === true;
    const trustMode = s.trust_mode || 'prior';
    const trustN = s.trust_n ?? 0;
    if (age == null) return html``;
    const stale = age > 5 || refreshMs > 3000;
    // P2.9.7 — trust regime of current meta-bucket. Renders glyph + n:
    //   prior     ○  no data, forecast is base+0 correction with prior σ
    //   shrunk    ◐  1 ≤ n < PRIOR_K=5, correction damped toward 0
    //   confident ●  n ≥ 5, full EWMA correction
    const trustGlyph = {prior: '○', shrunk: '◐', confident: '●'}[trustMode] || '?';
    return html`
      <div class="watch-pulse ${stale ? 'stale' : ''}">
        <span>watch <strong>${age.toFixed(1)}s</strong></span>
        <span>skips <strong>${skipped}</strong></span>
        <span>pulse <strong>${refreshMs.toFixed(0)}ms</strong></span>
        <span class="trust-${trustMode}" title="meta-bucket trust regime">
          ${trustGlyph} <strong>${trustMode}</strong> n=${trustN}
        </span>
        ${inflight ? html`<span class="inflight">⟳ refreshing</span>` : ''}
      </div>
    `;
  }

  _glyph(kind, cls = '') {
    const klass = `glyph ${cls}`;
    switch (kind) {
      case 'river':
        return html`<svg class="${klass}" viewBox="0 0 24 18" aria-hidden="true"><path d="M2 11c3-5 6 5 10 0s7-3 10 0"/></svg>`;
      case 'oath':
        return html`<svg class="${klass}" viewBox="0 0 24 18" aria-hidden="true"><path d="M2 14c3-7 7-9 11-7s6 6 9 3"/></svg>`;
      case 'veil':
        return html`<svg class="${klass}" viewBox="0 0 24 18" aria-hidden="true"><rect class="fill" x="2" y="4" width="20" height="10" rx="2"/><path d="M3 7c5 2 9-2 18 1M3 12c6-2 11 2 18-1"/></svg>`;
      case 'law':
        return html`<svg class="${klass}" viewBox="0 0 24 18" aria-hidden="true"><path d="M3 12c5-5 10-5 18-2"/></svg>`;
      case 'scar':
        return html`<svg class="${klass}" viewBox="0 0 24 18" aria-hidden="true"><circle cx="7" cy="12" r="3.5"/><path d="M10 10 18 5"/><circle cx="19" cy="5" r="1.4"/></svg>`;
      case 'margin':
        return html`<svg class="${klass}" viewBox="0 0 24 18" aria-hidden="true"><circle cx="7" cy="5" r="3.5"/><path d="M10 7 18 13"/><circle cx="19" cy="13" r="1.4"/></svg>`;
      case 'bound':
        return html`<svg class="${klass}" viewBox="0 0 24 18" aria-hidden="true"><circle cx="12" cy="9" r="6"/><path d="M7 9h10M12 4v10"/></svg>`;
      default:
        return html`<svg class="${klass}" viewBox="0 0 24 18" aria-hidden="true"><circle cx="12" cy="9" r="5"/></svg>`;
    }
  }

  _renderReadingMap({profile, buckets, logCount, corr, sigma, n, slopeStr, trend}) {
    const s = this.state || {};
    const cur = s.current || {};
    const t = this.displayT ?? cur.t;
    const predicted = this.displayPred ?? this._activePredicted();
    const delta = t != null && predicted != null ? predicted - t : null;
    const signed30 = this._err30sSigned();
    const passive = s.passive_residual_count_15m ?? 0;
    const controlled = s.controlled_residual_count_15m ?? 0;
    const fmtSigned = (v) => {
      if (v == null) return '—';
      const sign = v >= 0 ? '+' : '−';
      return `${sign}${Math.abs(v).toFixed(1)}°`;
    };
    const pct = cur.confidence != null
      ? `${(Math.max(0, Math.min(1, cur.confidence)) * 100).toFixed(1)}%`
      : '—';
    const deltaCls = delta == null
      ? ''
      : Math.abs(delta) < 0.2 ? 'cool' : delta > 0 ? 'warn' : 'cool';
    const runes = [
      ['river', 'gold', 'golden river', 'observed sensor trace'],
      ['oath', 'oath', 'cyan oath', 'live forecast path'],
      ['veil', 'veil', 'mist veil', 'uncertainty envelope'],
      ['law', 'law', 'raw inertia', 'current slope only'],
      ['scar', 'scar', 'hot scar', 'actual landed above oath'],
      ['margin', 'margin', 'cool margin', 'actual landed below oath'],
      ['bound', 'bound', 'bound seal', 'actuator overlap'],
    ];
    const ledger = [
      ['breath', t != null ? `${fmtNum(t, 1)}°C` : '—', trend.cls],
      ['next omen', fmtSigned(delta), deltaCls],
      ['dT/dt', slopeStr, ''],
      ['load tide', cur.load_slope != null ? `${cur.load_slope >= 0 ? '+' : ''}${cur.load_slope.toFixed(3)}/s` : '—', ''],
      ['oracle debt', corr ? `${corr}°C` : '—', ''],
      ['mist', sigma ? `${sigma}°C` : '—', ''],
      ['last residual', fmtSigned(signed30), this._signedResClass(signed30)],
      ['trust', pct, ''],
    ];
    return html`
      <section class="reading-map ${this.profileFlipHighlight ? 'flip' : ''}">
        <div class="map-head">
          <span class="map-kicker">chart augury</span>
          <span class="map-sub">heat path · model oath · machine debt</span>
          <span class="map-glyph-strip">
            ${this._glyph('river', 'gold')}
            ${this._glyph('oath', 'oath')}
            ${this._glyph('veil', 'veil')}
            ${this._glyph('scar', 'scar')}
            ${this._glyph('margin', 'margin')}
            ${this._glyph('bound', 'bound')}
          </span>
        </div>
        <div class="map-grid">
          <div class="trail-band">
            <div class="lane-head">
              ${this._glyph('scar', 'scar')}
              <strong>residual ledger</strong>
              <small>last twelve omens</small>
            </div>
            <div>
              ${this._renderResidualTrail()}
              <div class="trail-note">
                Actual minus prophecy. Hot scars landed above oath; cyan margins landed below it. Dashed seals overlap actuator work.
              </div>
            </div>
          </div>

          <div class="rune-board">
            <div class="lane-head">
              ${this._glyph('oath', 'oath')}
              <strong>runes of the chart</strong>
              <small>grammar</small>
            </div>
            <div class="rune-list">
              ${runes.map(([kind, cls, label, note]) => html`
                <div class="rune">
                  ${this._glyph(kind, cls)}
                  <span><b>${label}</b><span>${note}</span></span>
                </div>
              `)}
            </div>
          </div>

          <div class="ledger-board">
            <div class="lane-head">
              ${this._glyph('river', 'gold')}
              <strong>machine ledger</strong>
              <small>live covenant</small>
            </div>
            <div class="ledger-grid">
              ${ledger.map(([key, value, cls]) => html`
                <span class="ledger-cell ${cls}">
                  <span class="k">${key}</span>
                  <span class="v">${value}</span>
                </span>
              `)}
            </div>
            <div class="ledger-foot">
              <span>bucket ${n ? `n=${n}` : 'n=—'} · ${buckets ?? 0}</span>
              <span>trail ${passive} passive · ${controlled} sealed</span>
              <span>profile ${profile || '—'}</span>
              <span>archive ${logCount ?? 0}</span>
            </div>
            ${this._renderWatchPulse()}
          </div>
        </div>
      </section>
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
      <span class="hz-toggle" title="Display horizon — UI samples the meta-anchored saturation curve at the selected point. Requests beyond the predictor's native horizon (e.g. AlwaysIdleBaseline runs at +5s) are clamped to the at-horizon value rather than extrapolated.">
        <span class="hz-lbl">horizon · ${this._weightLabel()}</span>
        <span class="hz-seg-group">${items}</span>
      </span>
    `;
  }

  /* Scope toggle: how far back the canvas X-axis reaches.  Past-pred
     pins are re-binned by the backend to that window, so scope=30 keeps
     pins close to now (recent) while scope=120 lets the operator look
     two minutes back without manual rebinning. */
  _scopeToggle() {
    const items = PredictorCockpitTile.SCOPES.map((s) => {
      const active = s === this.activeScope;
      const label = s >= 60 ? `${s / 60}m` : `${s}s`;
      return html`
        <button
          class="hz-seg ${active ? 'active' : ''}"
          @click=${() => this._setScope(s)}
          title="Past window — ${s}s of validations"
        >${label}</button>
      `;
    });
    return html`
      <span class="hz-toggle" title="Past window on canvas — controls how far back validation pins distribute.">
        <span class="hz-lbl">scope</span>
        <span class="hz-seg-group">${items}</span>
      </span>
    `;
  }

  /* Pin-count toggle: [ 3 | 5 | 10 ] — how many past-prediction rings
     the canvas overlays.  Operator 2026-05-13 capped at 10 to keep
     the fan-from-now visualization readable. */
  _pinCountToggle() {
    const items = PredictorCockpitTile.PIN_COUNTS.map((n) => {
      const active = n === this.activePinCount;
      return html`
        <button
          class="hz-seg ${active ? 'active' : ''}"
          @click=${() => this._setPinCount(n)}
          title="Past-prediction pins on canvas — ${n}"
        >${n}</button>
      `;
    });
    return html`
      <span class="hz-toggle" title="How many past-prediction rings to overlay on the canvas. 3 = recent only, 10 = full window. All tether to the now-dot.">
        <span class="hz-lbl">pins</span>
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

        ${this._renderReadingMap({
          profile,
          buckets,
          logCount,
          corr: corrMatch?.[1],
          sigma: sigmaMatch?.[1],
          n: nMatch?.[1],
          slopeStr,
          trend,
        })}
      `,
    });
  }
}

customElements.define('predictor-cockpit-tile', PredictorCockpitTile);
