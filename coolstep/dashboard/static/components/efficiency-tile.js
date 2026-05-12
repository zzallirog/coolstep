import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame } from './_base.js';

/** Live + historical thermal-efficiency curve.
 *
 * Concept: на CMOS leakage экспоненциальна по T (Arrhenius). Производительность
 * на градус выше ambient = «КПД» — у каждого чипа sweet spot, потом резкий knee.
 *
 * Vertical: work_per_degree (load × freq / (T_chip - T_ambient))
 * Horizontal: cpu_temp_max bucket
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
      .legend {
        display: flex; gap: var(--sp-3); flex-wrap: wrap;
        font-size: 11px;
        color: var(--fg-muted);
      }
      .swatch {
        display: inline-block; width: 10px; height: 10px;
        border-radius: 2px; margin-right: 4px; vertical-align: middle;
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
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    this._timer = setInterval(() => this._refresh(), 30000);

    this._es = new EventSource('/api/sse/telemetry');
    this._es.onmessage = (ev) => {
      try {
        const f = JSON.parse(ev.data);
        // proxy: можем оценить из shortcut row, но без freq_avg/load_avg будет грубо;
        // используем raw через otdelnyi fetch, но тут — дёшево: только cpu_temp + work approximation
        this._fetchRawForLive(f);
      } catch (_e) {}
    };
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
    this._es?.close();
  }

  async _fetchRawForLive(_summary) {
    const data = await fetchJson('/api/telemetry/latest', null);
    if (!data?.raw) return;
    const cpu = data.raw.cpu;
    const loads = cpu.load_pct || [];
    const freqs = cpu.freq_mhz || [];
    if (!loads.length || !freqs.length) return;
    const loadAvg = loads.reduce((a, b) => a + b, 0) / loads.length;
    const freqAvg = freqs.reduce((a, b) => a + b, 0) / freqs.length;
    const tctl = (cpu.temps_c || {}).tctl || (cpu.temps_c || {}).tdie || 0;
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

    // Pull palette from the document — keeps the chart on-theme without
    // hardcoding hex. CSS vars live on :root, so we query the documentElement.
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
    ctx.font = '10px "Inter", sans-serif';
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
      ctx.font = '12px "Inter", sans-serif';
      ctx.fillText('No frames yet — historical curve fills as data accumulates.', 12, h / 2);
      return;
    }

    const maxEff = Math.max(...r.bins.map((b) => b.p95_efficiency)) || 1;

    // p95 fill + mean line per bin
    r.bins.forEach((b) => {
      const x0 = ((b.temp_low - tMin) / (tMax - tMin)) * w;
      const x1 = ((b.temp_high - tMin) / (tMax - tMin)) * w;
      const yMean = h - (b.mean_efficiency / maxEff) * (h - 12);
      const yP95 = h - (b.p95_efficiency / maxEff) * (h - 12);
      // rgba via global alpha — works for hex too
      ctx.globalAlpha = 0.22;
      ctx.fillStyle = cool;
      ctx.fillRect(x0, yP95, x1 - x0 - 1, h - yP95);
      ctx.globalAlpha = 1;
      ctx.fillStyle = cool;
      ctx.fillRect(x0, yMean, x1 - x0 - 1, 2);
    });

    // Sweet spot marker
    if (r.sweet_spot_temp != null) {
      const x = ((r.sweet_spot_temp - tMin) / (tMax - tMin)) * w;
      ctx.strokeStyle = ok;
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = ok;
      ctx.fillText(`sweet ${r.sweet_spot_temp.toFixed(0)}°`, x + 4, 12);
    }

    // Knee marker
    if (r.knee_temp != null) {
      const x = ((r.knee_temp - tMin) / (tMax - tMin)) * w;
      ctx.strokeStyle = warn;
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = warn;
      ctx.fillText(`knee ${r.knee_temp.toFixed(0)}°`, x + 4, 24);
    }

    // Live point — pulse ring then dot
    if (this.livePoint) {
      const x = ((this.livePoint.temp - tMin) / (tMax - tMin)) * w;
      const y = h - (this.livePoint.eff / maxEff) * (h - 12);
      ctx.globalAlpha = 0.35;
      ctx.fillStyle = warn;
      ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = 1;
      ctx.fillStyle = warn;
      ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
    }
  }

  render() {
    const r = this.report || {};
    return renderFrame({
      title: 'Thermal efficiency',
      meta: `${fmtNum(r.sample_count, 0)} samples`,
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">live now</span>
            <span class="metric cat">${this.livePoint
              ? html`${fmtNum(this.livePoint.temp, 1)}<small>°C</small>`
              : '—'}</span>
          </div>
          <div class="col">
            <span class="eyebrow">work / °C</span>
            <span class="metric">${this.livePoint ? fmtNum(this.livePoint.eff, 2) : '—'}</span>
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
          <span><span class="swatch" style="background:var(--cat-cooling)"></span>mean</span>
          <span><span class="swatch" style="background:color-mix(in srgb, var(--cat-cooling) 40%, transparent)"></span>p95</span>
          <span><span class="swatch" style="background:var(--ok)"></span>sweet</span>
          <span><span class="swatch" style="background:var(--warn)"></span>knee · live</span>
        </div>
        <p class="muted">
          Y = work per °C above ambient · X = T_chip · sweet=peak / knee=&lt;70% peak.
          Концепт → docs/efficiency-curve.md
        </p>
      `,
    });
  }
}

customElements.define('efficiency-tile', EfficiencyTile);
