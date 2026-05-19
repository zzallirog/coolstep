import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame } from './_base.js';

/** Live + historical thermal-efficiency curve.
 *
 * Concept: на CMOS leakage экспоненциальна по T (Arrhenius). Производительность
 * на градус выше ambient = «КПД» — у каждого чипа sweet spot, потом резкий knee.
 *
 * Vertical: work_per_degree (load × freq / (T_chip - T_ambient))
 * Horizontal: cpu_temp_max bucket
 *
 * NOTE: The predicted-dot / forward-marker / countdown / pulse used to live
 * here on top of the histogram.  ADR-018 moved that whole apparatus to its
 * own dedicated tile (<predictor-cockpit-tile>) — efficiency tile is now
 * purely the historical curve + a single live dot showing where we are on it.
 */
export class EfficiencyTile extends LitElement {
  static styles = [
    tileBaseStyles,
    css`
      :host { grid-column: span 2; }
      canvas {
        width: 100%;
        height: 200px;
        display: block;
        background: color-mix(in srgb, var(--bg) 70%, transparent);
        border-radius: var(--radius-sm);
        border: 1px solid var(--border-soft);
      }

      /* Relational subline under WORK/°C — same shape as the Δ block
         in predictor-cockpit so the visual language stays consistent
         across the two graph-category tiles. */
      .delta {
        font-family: var(--font-mono, monospace);
        font-size: 11px;
        letter-spacing: var(--track-pill, 0.08em);
        color: var(--fg-muted);
        margin-top: 4px;
      }
      .delta .v {
        font-variant-numeric: tabular-nums;
        font-weight: 600;
        font-size: 13px;
      }
      .delta.ok   { color: var(--ok); }
      .delta.warn { color: var(--warn); }
      .delta.err  { color: var(--err); }
      .delta.zero { color: var(--fg-dim); }

      /* Legend strip — chip pattern lifted from predictor-cockpit so
         the visual vocabulary is shared.  Swatch widths/heights match
         so the eye reads the two tiles as one chart-family. */
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
      .legend .sw.bar-mean { background: var(--cat-cooling, #7be0d4); }
      .legend .sw.bar-p95  { background: color-mix(in srgb, var(--cat-cooling, #7be0d4) 30%, transparent); }
      .legend .sw.line-sweet {
        background: linear-gradient(to right,
          var(--ok) 0 7px, transparent 7px 11px,
          var(--ok) 11px 18px, transparent 18px 22px);
      }
      .legend .sw.line-knee {
        background: linear-gradient(to right,
          var(--warn) 0 7px, transparent 7px 11px,
          var(--warn) 11px 18px, transparent 18px 22px);
      }
      .legend .sw.dot-live {
        width: 12px; height: 12px; border-radius: 50%;
        background: var(--warn);
        box-shadow: 0 0 0 3px color-mix(in srgb, var(--warn) 30%, transparent);
      }
    `,
  ];

  static properties = {
    report: { state: true },
    livePoint: { state: true },
  };

  constructor() {
    super();
    this.report = null;
    this.livePoint = null;
    this._lastSeenProfile = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    this._timer = setInterval(() => this._refresh(), 30000);

    // Balance-plan step IV: SSE replaced by 1Hz polling (see live-telemetry-tile).
    this._lastSeenTs = null;
    this._sseTimer = setInterval(async () => {
      const data = await fetchJson('/api/telemetry/latest', null);
      if (data && data.ts !== this._lastSeenTs) {
        this._lastSeenTs = data.ts;
        this._fetchRawForLive(data);
      }
    }, 1000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
    clearInterval(this._sseTimer);
  }

  async _fetchRawForLive(_summary) {
    const [data, ml] = await Promise.all([
      fetchJson('/api/telemetry/latest', null),
      fetchJson('/api/ml-state', null),
    ]);
    if (!data?.raw) return;

    // Phase 4: invalidate the report cache on tuned-profile flip — the
    // histogram's sweet/knee/p95 are profile-dependent; without this they
    // stale for up to 30 s after a flip.
    const newProfile = ml?.active_tuned_profile ?? null;
    if (
      this._lastSeenProfile !== null
      && newProfile !== this._lastSeenProfile
    ) {
      this._refresh();  // re-fetch /api/efficiency
    }
    this._lastSeenProfile = newProfile;

    const cpu = data.raw.cpu;
    const loads = cpu.load_pct || [];
    const freqs = cpu.freq_mhz || [];
    if (!loads.length || !freqs.length) return;
    const loadAvg = loads.reduce((a, b) => a + b, 0) / loads.length;
    const freqAvg = freqs.reduce((a, b) => a + b, 0) / freqs.length;
    // Universal cpu_temp shortcut (G-9): tctl → tdie → package → max-of-cores.
    const t = cpu.temps_c || {};
    const cores = Object.entries(t)
      .filter(([k]) => /^core /.test(k))
      .map(([, v]) => v);
    const tctl = t.tctl || t.tdie || t.package || t['x86_pkg_temp']
                 || (cores.length ? Math.max(...cores) : 0);
    if (tctl <= (this.report?.t_ambient ?? 30)) return;
    const headroom = Math.max(1, tctl - (this.report?.t_ambient ?? 30));
    const work = (loadAvg / 100) * (freqAvg / 1000) * 100;
    this.livePoint = { temp: tctl, eff: work / headroom };
    this.updateComplete.then(() => this._draw());
  }

  async _refresh() {
    this.report = await fetchJson('/api/efficiency?since=7d',
                                  { bins: [], sample_count: 0, t_ambient: 30, t_max: 100 });
    this.updateComplete.then(() => this._draw());
  }

  _draw() {
    const cnv = this.renderRoot?.querySelector('canvas');
    if (!cnv) return;
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
    const dim  = cs.getPropertyValue('--fg-dim').trim() || '#58627c';
    const grid = cs.getPropertyValue('--border').trim() || 'rgba(255,255,255,0.07)';

    const r = this.report || { bins: [], t_ambient: 30, t_max: 100 };
    const tMin = r.t_ambient;
    const tMax = r.t_max;

    // Axes
    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;
    ctx.fillStyle = dim;
    ctx.font = '10px var(--font-mono, monospace)';
    for (let t = Math.ceil(tMin / 10) * 10; t <= tMax; t += 10) {
      const x = ((t - tMin) / (tMax - tMin)) * w;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h);
      ctx.stroke();
      ctx.fillText(`${t}°`, x + 2, h - 2);
    }

    if (!r.bins.length) {
      ctx.fillStyle = dim;
      ctx.font = '11px var(--font-mono, monospace)';
      ctx.fillText('No frames yet — historical curve fills as data accumulates.', 12, h / 2);
      return;
    }

    const maxEff = Math.max(...r.bins.map((b) => b.p95_efficiency)) || 1;

    r.bins.forEach((b) => {
      const x0 = ((b.temp_low - tMin) / (tMax - tMin)) * w;
      const x1 = ((b.temp_high - tMin) / (tMax - tMin)) * w;
      const yMean = h - (b.mean_efficiency / maxEff) * (h - 12);
      const yP95 = h - (b.p95_efficiency / maxEff) * (h - 12);
      ctx.globalAlpha = 0.22;
      ctx.fillStyle = cool;
      ctx.fillRect(x0, yP95, x1 - x0 - 1, h - yP95);
      ctx.globalAlpha = 1;
      ctx.fillStyle = cool;
      ctx.fillRect(x0, yMean, x1 - x0 - 1, 2);
    });

    if (r.sweet_spot_temp != null) {
      const x = ((r.sweet_spot_temp - tMin) / (tMax - tMin)) * w;
      ctx.strokeStyle = ok;
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = ok;
      ctx.fillText(`sweet ${r.sweet_spot_temp.toFixed(0)}°`, x + 4, 12);
    }

    if (r.knee_temp != null) {
      const x = ((r.knee_temp - tMin) / (tMax - tMin)) * w;
      ctx.strokeStyle = warn;
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = warn;
      ctx.fillText(`knee ${r.knee_temp.toFixed(0)}°`, x + 4, 24);
    }

    // Single live point — match the cockpit "now" dot: halo + solid
    // core, warn colour.  Same radius (8 / 3.5) so the two tiles read
    // as one instrument family.
    if (this.livePoint) {
      const x = ((this.livePoint.temp - tMin) / (tMax - tMin)) * w;
      const y = h - (this.livePoint.eff / maxEff) * (h - 12);
      ctx.fillStyle = warn;
      ctx.globalAlpha = 0.35;
      ctx.beginPath(); ctx.arc(x, y, 8, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = 1;
      ctx.beginPath(); ctx.arc(x, y, 3.5, 0, Math.PI * 2); ctx.fill();
    }
  }

  /* Relational subline: where are we on the curve right now?  Expresses
     temperature through the reference points the curve already names —
     sweet (peak efficiency) and knee (efficiency collapse).  No naked
     °C number; same "one through another" framing as predictor-cockpit. */
  _sweetDelta() {
    const r = this.report || {};
    const lp = this.livePoint;
    if (!lp || r.sweet_spot_temp == null) {
      return { label: '—', detail: '', cls: 'zero' };
    }
    const dSweet = lp.temp - r.sweet_spot_temp;
    if (r.knee_temp != null && lp.temp >= r.knee_temp) {
      return { label: 'past knee', detail: `+${(lp.temp - r.knee_temp).toFixed(0)}° over`, cls: 'err' };
    }
    if (Math.abs(dSweet) < 1.5) {
      return { label: 'at sweet', detail: `${lp.temp.toFixed(0)}°`, cls: 'ok' };
    }
    if (dSweet < 0) {
      return { label: 'below sweet', detail: `${dSweet.toFixed(1)}°`, cls: 'zero' };
    }
    return { label: 'above sweet', detail: `+${dSweet.toFixed(1)}°`, cls: 'warn' };
  }

  render() {
    const r = this.report || {};
    const sweet = this._sweetDelta();
    return renderFrame({
      title: 'Thermal efficiency',
      meta: `${fmtNum(r.sample_count, 0)} samples`,
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">work / °C now</span>
            <span class="metric cat">${this.livePoint
              ? fmtNum(this.livePoint.eff, 2)
              : '—'}</span>
            <div class="delta ${sweet.cls}">
              <span class="v">${sweet.label}</span>${sweet.detail ? html` ${sweet.detail}` : ''}
            </div>
          </div>
        </div>

        <div class="mini-grid">
          <div class="mini ok">
            <span class="eyebrow">sweet spot</span>
            <span class="v ok">${r.sweet_spot_temp != null
              ? html`${fmtNum(r.sweet_spot_temp, 1)}<small>°C</small>`
              : html`fitting<small>${r.sample_count ? ` · ${r.sample_count} samples` : ''}</small>`}</span>
          </div>
          <div class="mini warn">
            <span class="eyebrow">knee</span>
            <span class="v warn">${r.knee_temp != null
              ? html`${fmtNum(r.knee_temp, 1)}<small>°C</small>`
              : '—'}</span>
          </div>
          <div class="mini">
            <span class="eyebrow">T_ambient</span>
            <span class="v">${fmtNum(r.t_ambient, 0)}<small>°C</small></span>
          </div>
          <div class="mini">
            <span class="eyebrow">bins</span>
            <span class="v">${r.bins?.length || 0}</span>
          </div>
        </div>

        <canvas></canvas>

        <div class="legend">
          <span class="item"><span class="sw bar-mean"></span>mean work/°C</span>
          <span class="item"><span class="sw bar-p95"></span>p95</span>
          <span class="item"><span class="sw line-sweet"></span>sweet (peak)</span>
          <span class="item"><span class="sw line-knee"></span>knee (≤70% peak)</span>
          <span class="item"><span class="sw dot-live"></span>now</span>
        </div>
      `,
    });
  }
}

customElements.define('efficiency-tile', EfficiencyTile);
