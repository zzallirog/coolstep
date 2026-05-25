// Shared Lit base + frame template + small fetch helper for coolstep tiles.
// Visual envelope ports atrium's glass theme into vanilla-Lit shadow DOM:
// frosted-blur cards over aurora bg, inset specular highlight, two-layer
// gradient sheen tinted by --cat-color, hero metric typography. Tiles call
// renderFrame({title, eyebrow?, meta?, body}). Body slot uses the .hero /
// .eyebrow / .mini-grid / .meter / .stat-pill utility classes below.

import { LitElement, html, css, nothing } from '/static/vendor/lit.min.js';

export { LitElement, html, css, nothing };

export async function fetchJson(url, fallback = null, { retry = 1, retryDelayMs = 250 } = {}) {
  // Tiles refresh on long-ish intervals (3-30 s). A single transient fetch
  // failure during e.g. a daemon restart used to paint the entire tile as
  // empty. One retry on a short delay catches the common case without being
  // a circuit-breaker — final failure still falls back.
  for (let attempt = 0; attempt <= retry; attempt += 1) {
    try {
      const r = await fetch(url, { cache: 'no-store' });
      if (r.ok) return await r.json();
    } catch (_e) { /* fallthrough */ }
    if (attempt < retry) await new Promise((res) => setTimeout(res, retryDelayMs));
  }
  return fallback;
}

export function fmtNum(v, digits = 1, suffix = '') {
  if (v == null || isNaN(v)) return '—';
  return Number(v).toFixed(digits) + suffix;
}

/* The frame template every tile renders into. */
export function renderFrame({ title, meta = null, body }) {
  return html`
    <article class="frame">
      <header>
        <h2>${title}</h2>
        ${meta != null && meta !== ''
          ? html`<span class="meta">${meta}</span>`
          : nothing}
      </header>
      <div class="body">${body}</div>
    </article>
  `;
}

export const tileBaseStyles = css`
  /* ───────────────────────────────────────────────────────────────────
   * INSTRUMENT — tile chrome (v0.4.0).
   *
   * Every tile is rendered as a journal-figure card with crosshair
   * registration marks at the corners, a figure-style header
   * (§NN counter + title + monospace meta chip), and body utilities
   * that compose the rest of the interface vocabulary.
   *
   * Selector contract is fixed — components rely on these class names:
   *   .frame  .body  .hero  .col  .metric  .aside  .eyebrow
   *   .mini-grid  .mini  .v
   *   .meter  .meter-fill  .meter-label
   *   .kv    .stat-pill  .dot  .section-h
   *   .empty .error-strip .muted .num .mono .skel
   * The values here change with theme; the names do not.
   * ─────────────────────────────────────────────────────────────────── */
  :host {
    display: block;
    color: var(--fg, #e6ddc4);
    font-family: var(--font-sans, "IBM Plex Sans", system-ui, sans-serif);
    --cat-color: var(--accent, #7be0d4);
    --card-min-h: var(--card-min-h, 208px);
    min-height: var(--card-min-h);
    container-type: inline-size;
    contain: layout paint style;
    animation: card-mount var(--dur-slow, 520ms) var(--ease-step, cubic-bezier(0.16, 0.84, 0.44, 1)) both;
    animation-delay: calc(var(--idx, 0) * 48ms);
  }

  :host([data-cat="thermal"]) { --cat-color: var(--cat-thermal, #ff6a3d); }
  :host([data-cat="cooling"]) { --cat-color: var(--cat-cooling, #7be0d4); }
  :host([data-cat="graph"])   { --cat-color: var(--cat-graph,   #d4a14a); }
  :host([data-cat="events"])  { --cat-color: var(--cat-events,  #b5c8e8); }
  :host([data-cat="system"])  { --cat-color: var(--cat-system,  #8a8576); }

  /* Mount: blueprint-line reveal — the tile is "drawn" left-to-right
   * with a clip-path inset and a faint vertical translation, instead
   * of fading from blur. Reads as a pen stroke across the page. */
  @keyframes card-mount {
    from { opacity: 0; transform: translateY(8px); clip-path: inset(0 100% 0 0); }
    to   { opacity: 1; transform: translateY(0);   clip-path: inset(0 0     0 0); }
  }
  @media (prefers-reduced-motion: reduce) {
    :host { animation: none; }
  }

  /* ── Frame: a paper-flat card with hairline rule and corner marks ── */
  .frame {
    position: relative;
    height: 100%;
    box-sizing: border-box;
    border: 1px solid var(--border, rgba(230, 221, 196, 0.10));
    /* No glass blur — the tile sits flat on the page, not floating. */
    background: var(--card-bg, color-mix(in srgb, #11161e 92%, transparent));
    /* A single category-tinted thermal stripe down the left edge —
     * thin, near-invisible at rest, brightens on hover. */
    box-shadow:
      inset 2px 0 0 color-mix(in srgb, var(--cat-color) 0%, transparent),
      var(--shadow-card,
        0 1px 0 rgba(0, 0, 0, 0.5),
        inset 0 1px 0 rgba(255, 255, 255, 0.018));
    border-radius: var(--card-radius, 3px);
    padding: var(--card-padding-y, 26px) var(--card-padding-x, 24px);
    filter: saturate(var(--card-idle-saturate, 0.88));
    opacity: var(--card-idle-opacity, 0.94);
    overflow: hidden;
    transition:
      border-color var(--dur-fast, 120ms) var(--ease-out),
      box-shadow   var(--dur-med,  220ms) var(--ease-out),
      transform    var(--dur-med,  220ms) var(--ease-out),
      filter       var(--dur-med,  220ms) var(--ease-out),
      opacity      var(--dur-med,  220ms) var(--ease-out);
  }

  /* Diagonal pair of drafting registration marks — top-left and
   * bottom-right corners of every tile carry a small L crosshair.
   * Single most distinctive trait of the visual language, drawn as
   * pseudo-element borders so they stay crisp at any DPR. */
  .frame::before,
  .frame::after {
    content: "";
    position: absolute;
    width: 10px;
    height: 10px;
    pointer-events: none;
    border: 1px solid var(--rule-tick, rgba(230, 221, 196, 0.34));
  }
  .frame::before {
    top: 5px; left: 5px;
    border-right: none; border-bottom: none;
  }
  .frame::after {
    bottom: 5px; right: 5px;
    border-left: none; border-top: none;
  }

  .frame > * { position: relative; z-index: 1; }

  :host(:hover) .frame,
  :host(:focus-within) .frame {
    filter: saturate(1);
    opacity: 1;
    border-color: color-mix(in srgb, var(--cat-color) 55%, var(--border-strong, rgba(230,221,196,0.22)));
    box-shadow:
      inset 2px 0 0 var(--cat-color),
      var(--shadow-card-hover,
        0 18px 44px rgba(0, 0, 0, 0.55),
        inset 0 1px 0 rgba(255, 255, 255, 0.04));
    transform: translateY(var(--card-hover-translate, -1px));
  }

  /* ── Header: §NN figure caption + serif italic title + meta chip ── */
  header {
    display: grid;
    grid-template-columns: auto 1fr auto;
    align-items: baseline;
    gap: var(--sp-3, 16px);
    margin-bottom: var(--sp-3, 16px);
    min-width: 0;
    padding-bottom: var(--sp-2, 8px);
    border-bottom: 1px solid var(--border-soft, rgba(230, 221, 196, 0.05));
  }
  /* The "FIG —" tag at the start of every tile header. Acts as a fixed
   * figure-caption mark in the journal vocabulary. The numbering would
   * normally come from a CSS counter, but counter values can't be fed
   * from CSS custom properties (and the grid counter doesn't pierce
   * shadow DOM), so we keep the label static — the actual sequence is
   * already visible in the page itself. */
  header::before {
    content: "FIG —";
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: var(--track-pill, 0.08em);
    color: var(--cat-color);
    padding: 3px 8px;
    border: 1px solid color-mix(in srgb, var(--cat-color) 38%, transparent);
    border-radius: var(--radius-chip, 1px);
    align-self: center;
    line-height: 1;
    text-transform: uppercase;
    font-variant-numeric: tabular-nums;
  }
  h2 {
    margin: 0;
    font-family: var(--font-display, "Instrument Serif", Georgia, serif);
    font-size: var(--font-size-lg, 18px);
    font-style: italic;
    font-weight: 400;
    letter-spacing: -0.005em;
    line-height: 1.1;
    color: var(--fg-strong, #f3ecd4);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }
  .meta {
    color: var(--fg-muted, #8d9aa9);
    font-family: var(--font-mono, "IBM Plex Mono", monospace);
    font-size: var(--font-size-xs, 10.5px);
    font-weight: 400;
    letter-spacing: var(--track-pill, 0.08em);
    text-transform: uppercase;
    flex-shrink: 0;
    white-space: nowrap;
    padding: 3px 8px;
    background: color-mix(in srgb, var(--cat-color) 8%, transparent);
    border: 1px solid color-mix(in srgb, var(--cat-color) 24%, transparent);
    border-radius: var(--radius-chip, 1px);
    align-self: center;
  }
  .meta .stale { color: var(--err, #ff6a3d); font-style: italic; }

  /* ── Body ───────────────────────────────────────────────────────── */
  .body {
    font-size: var(--font-size-sm, 12.5px);
    color: var(--fg, #e6ddc4);
    display: flex;
    flex-direction: column;
    gap: var(--sp-3, 16px);
    line-height: 1.55;
  }

  /* ── Hero metric: large italic serif numerals, instrument-readout ── */
  .hero {
    display: flex;
    align-items: baseline;
    gap: var(--sp-4, 24px);
    flex-wrap: wrap;
    padding: var(--sp-1, 4px) 0;
  }
  .hero .col {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
    position: relative;
  }
  .hero .col + .col {
    padding-left: var(--sp-4, 24px);
    /* A thin vertical rule, drawn as a left border, terminates with two
     * small tick marks (top/bottom) — like the gridlines on a scope. */
    border-left: 1px solid var(--border, rgba(230,221,196,0.10));
  }
  .hero .col + .col::before,
  .hero .col + .col::after {
    content: "";
    position: absolute;
    left: -1px;
    width: 5px;
    height: 1px;
    background: var(--rule-tick, rgba(230,221,196,0.34));
  }
  .hero .col + .col::before { top: 4px; }
  .hero .col + .col::after { bottom: 4px; }

  .hero .metric {
    font-family: var(--font-display, "Instrument Serif", Georgia, serif);
    font-style: italic;
    font-weight: var(--font-weight-hero, 400);
    font-size: clamp(2.4rem, 8.5cqw, 3.6rem);
    line-height: 0.95;
    color: var(--fg-strong, #f3ecd4);
    font-variant-numeric: tabular-nums lining-nums;
    letter-spacing: -0.02em;
  }
  .hero .metric small {
    font-family: var(--font-mono, monospace);
    font-style: normal;
    font-size: 0.32em;
    margin-left: 6px;
    color: var(--fg-muted, #8d9aa9);
    font-weight: 400;
    letter-spacing: var(--track-pill, 0.08em);
    text-transform: uppercase;
    vertical-align: 0.4em;
  }
  .hero .metric.cat   { color: var(--cat-color); }
  .hero .metric.ok    { color: var(--ok,   #6fcaa9); }
  .hero .metric.warn  { color: var(--warn, #e6b25c); }
  .hero .metric.err   { color: var(--err,  #ff6a3d); }
  .hero .aside {
    margin-left: auto;
    display: flex;
    flex-direction: column;
    gap: 4px;
    align-items: flex-end;
  }

  /* Eyebrow — instrument label, all-caps mono with wide tracking */
  .eyebrow {
    color: var(--fg-muted, #8d9aa9);
    font-family: var(--font-mono, monospace);
    font-size: 10px;
    font-weight: 500;
    letter-spacing: var(--track-eyebrow, 0.22em);
    text-transform: uppercase;
    line-height: 1.2;
  }
  .eyebrow.cat { color: var(--cat-color); }
  .eyebrow::before {
    content: "› ";
    color: var(--cat-color);
    opacity: 0.7;
  }

  /* ── Mini-grid: nested data wells ───────────────────────────────── */
  .mini-grid {
    display: grid;
    gap: 1px;  /* hairline gap = the gap IS the rule */
    grid-template-columns: repeat(auto-fit, minmax(128px, 1fr));
    background: var(--border-soft, rgba(230,221,196,0.05));
    border: 1px solid var(--border-soft, rgba(230,221,196,0.05));
  }
  .mini {
    padding: 10px 12px;
    border: none;
    border-radius: 0;
    background: color-mix(in srgb, var(--paper-2, #161c27) 86%, transparent);
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
    position: relative;
  }
  /* Categorical mini uses an inset 2px left rule, not a coloured border */
  .mini.cat  { box-shadow: inset 2px 0 0 var(--cat-color); }
  .mini.ok   { box-shadow: inset 2px 0 0 var(--ok); }
  .mini.warn { box-shadow: inset 2px 0 0 var(--warn); }
  .mini.err  { box-shadow: inset 2px 0 0 var(--err); }

  .mini .v {
    font-family: var(--font-display, "Instrument Serif", Georgia, serif);
    font-style: italic;
    font-weight: 400;
    font-size: clamp(1.3rem, 4.6cqw, 1.7rem);
    line-height: 1;
    color: var(--fg-strong, #f3ecd4);
    font-variant-numeric: tabular-nums lining-nums;
    letter-spacing: -0.015em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .mini .v small {
    font-family: var(--font-mono, monospace);
    font-style: normal;
    font-size: 0.5em;
    margin-left: 4px;
    color: var(--fg-muted, #8d9aa9);
    font-weight: 400;
    letter-spacing: var(--track-pill, 0.08em);
    text-transform: uppercase;
  }
  .mini .v.ok    { color: var(--ok,   #6fcaa9); }
  .mini .v.warn  { color: var(--warn, #e6b25c); }
  .mini .v.err   { color: var(--err,  #ff6a3d); }
  .mini .v.cat   { color: var(--cat-color); }

  /* ── Meter: a horizontal bar with hairline scale ticks ─────────── */
  .meter {
    position: relative;
    height: 24px;
    border-radius: var(--radius-sm, 2px);
    overflow: hidden;
    background: color-mix(in srgb, var(--paper-2, #161c27) 80%, transparent);
    box-shadow: inset 0 0 0 1px var(--border, rgba(230,221,196,0.10));
  }
  /* Tick marks every 10% across the meter — drawn as a repeating linear
   * gradient so they're "etched" into the bar rather than overlaid. */
  .meter::before {
    content: "";
    position: absolute;
    inset: 0;
    background-image: repeating-linear-gradient(90deg,
      transparent 0,
      transparent calc(10% - 1px),
      var(--border-soft, rgba(230,221,196,0.05)) calc(10% - 1px),
      var(--border-soft, rgba(230,221,196,0.05)) 10%);
    pointer-events: none;
    z-index: 2;
  }
  .meter-fill {
    position: absolute;
    inset: 0 auto 0 0;
    border-radius: inherit;
    background: linear-gradient(90deg,
      color-mix(in srgb, var(--cat-color) 55%, transparent),
      var(--cat-color));
    transition: width var(--dur-med, 220ms) var(--ease-out);
  }
  .meter-label {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: var(--font-mono, monospace);
    font-weight: 500;
    font-size: var(--font-size-sm, 12.5px);
    color: var(--fg-strong, #f3ecd4);
    font-variant-numeric: tabular-nums;
    letter-spacing: var(--track-pill, 0.08em);
    text-shadow: 0 1px 0 rgba(0, 0, 0, 0.5);
    z-index: 3;
  }

  /* ── KV pair (label · value, dotted rule between) ──────────────── */
  .kv {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: var(--sp-2, 8px);
    padding: 5px 0;
    font-size: var(--font-size-sm, 12.5px);
    border-bottom: 1px dotted var(--border-soft, rgba(230,221,196,0.05));
  }
  .kv:last-child { border-bottom: none; }
  .kv span {
    color: var(--fg-muted, #8d9aa9);
    font-family: var(--font-mono);
    font-size: 10.5px;
    letter-spacing: var(--track-pill, 0.08em);
    text-transform: uppercase;
  }
  .kv strong {
    color: var(--fg-strong, #f3ecd4);
    font-weight: 500;
    font-family: var(--font-mono, monospace);
    font-variant-numeric: tabular-nums;
    text-align: right;
  }

  /* ── Stat pill: small rectangular nameplate chip ───────────────── */
  .stat-pill {
    display: inline-flex;
    align-items: center;
    gap: var(--sp-1, 4px);
    height: 20px;
    padding: 0 9px;
    border-radius: var(--radius-chip, 1px);
    font-family: var(--font-mono, monospace);
    font-size: 10.5px;
    letter-spacing: var(--track-pill, 0.08em);
    text-transform: uppercase;
    font-variant-numeric: tabular-nums;
    background: color-mix(in srgb, var(--paper-2, #161c27) 70%, transparent);
    border: 1px solid var(--border, rgba(230,221,196,0.10));
    border-left: 2px solid var(--fg-dim, #5b6678);
    color: var(--fg-muted, #8d9aa9);
  }
  .stat-pill.ok   { color: var(--ok);   border-left-color: var(--ok); }
  .stat-pill.warn { color: var(--warn); border-left-color: var(--warn); }
  .stat-pill.err  { color: var(--err);  border-left-color: var(--err); background: var(--err-soft, rgba(255,106,61,0.12)); }
  .stat-pill.cat  { color: var(--cat-color); border-left-color: var(--cat-color); }

  /* ── Status indicator — square (engraved LED), not a circle ────── */
  .dot {
    display: inline-block;
    width: 8px; height: 8px;
    border-radius: 0;
    margin-right: var(--sp-2, 8px);
    vertical-align: middle;
    background: var(--fg-dim, #5b6678);
    box-shadow: 0 0 0 1px var(--border-strong, rgba(230,221,196,0.22));
  }
  .dot.ok   { background: var(--ok);   box-shadow: 0 0 0 1px var(--ok),   0 0 8px color-mix(in srgb, var(--ok)   55%, transparent); animation: dot-beat 2.4s ease-in-out infinite; }
  .dot.warn { background: var(--warn); box-shadow: 0 0 0 1px var(--warn), 0 0 8px color-mix(in srgb, var(--warn) 55%, transparent); animation: dot-beat 1.6s ease-in-out infinite; }
  .dot.err  { background: var(--err);  box-shadow: 0 0 0 1px var(--err),  0 0 10px color-mix(in srgb, var(--err)  70%, transparent); animation: dot-beat 1.0s ease-in-out infinite; }
  .dot.cat  { background: var(--cat-color); box-shadow: 0 0 0 1px var(--cat-color), 0 0 8px color-mix(in srgb, var(--cat-color) 55%, transparent); }

  @keyframes dot-beat {
    0%, 100% { opacity: 1; }
    50%      { opacity: 0.55; }
  }
  @media (prefers-reduced-motion: reduce) {
    .dot.ok, .dot.warn, .dot.err { animation: none; }
  }

  /* ── Section heading inside body ───────────────────────────────── */
  .section-h {
    margin: 0;
    color: var(--fg-muted, #8d9aa9);
    font-family: var(--font-mono, monospace);
    font-size: 10px;
    font-weight: 500;
    letter-spacing: var(--track-eyebrow, 0.22em);
    text-transform: uppercase;
    padding-bottom: 4px;
    border-bottom: 1px dashed var(--border-soft, rgba(230,221,196,0.05));
  }

  /* ── Empty / error / muted strips ──────────────────────────────── */
  .muted {
    color: var(--fg-muted, #8d9aa9);
    font-size: var(--font-size-xs, 10.5px);
    margin: 0;
    font-family: var(--font-mono);
    letter-spacing: var(--track-pill, 0.08em);
  }
  .empty {
    padding: var(--sp-4, 24px) var(--sp-3, 16px);
    color: var(--fg-muted, #8d9aa9);
    font-size: var(--font-size-xs, 10.5px);
    background: transparent;
    border-radius: 0;
    border: 1px dashed var(--border, rgba(230,221,196,0.10));
    font-family: var(--font-mono);
    text-align: center;
    letter-spacing: var(--track-eyebrow, 0.22em);
    text-transform: uppercase;
  }
  .error-strip {
    padding: var(--sp-2, 8px) var(--sp-3, 16px);
    color: var(--err, #ff6a3d);
    font-size: var(--font-size-sm, 12.5px);
    background: var(--err-soft, rgba(255,106,61,0.12));
    border-radius: 0;
    border-left: 2px solid var(--err);
    font-family: var(--font-mono);
  }

  /* ── Tabular niceties for numeric content ──────────────────────── */
  .num  { font-variant-numeric: tabular-nums lining-nums; font-feature-settings: "tnum"; }
  .mono { font-family: var(--font-mono, monospace); font-size: 12px; letter-spacing: 0.01em; }

  /* ── Skeleton shimmer (in-shadow copy) ─────────────────────────── */
  .skel {
    display: inline-block;
    background: linear-gradient(
      90deg,
      var(--paper-2, #161c27) 0%,
      var(--paper-3, #1d2532) 50%,
      var(--paper-2, #161c27) 100%
    );
    background-size: 200px 100%;
    background-repeat: no-repeat;
    border-radius: var(--radius-sm, 2px);
    animation: skel-shimmer 1.4s ease-in-out infinite;
    height: 1em;
    width: 100%;
    min-height: 14px;
  }
  @keyframes skel-shimmer {
    0%   { background-position: -200px 0; }
    100% { background-position: calc(100% + 200px) 0; }
  }
  @media (prefers-reduced-motion: reduce) {
    .skel { animation: none; }
  }
`;
