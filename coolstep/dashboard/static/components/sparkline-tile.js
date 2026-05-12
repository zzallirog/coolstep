import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame } from './_base.js';

/** Three sparklines (CPU temp / GPU temp / Fan max RPM) over /api/telemetry/range.
 * Plain Canvas, no chart library — keeps deps zero, atrium-style minimal.
 *
 * Bias overlay: a 4th visual layer (drawn BEHIND the data lines) shows when an
 * actuator bias was applied. Bands span applied_at → min(expires_at, window_end)
 * with height proportional to intensity_pct. Data comes from /api/actuator-journal.
 * This makes the "coolstep moved first" story legible: fan RPM ramps during the
 * green band before the temp climbs into the danger zone. */
export class SparklineTile extends LitElement {
  static styles = [
    tileBaseStyles,
    css`
      :host { grid-column: span 2; }
      .row {
        display: grid;
        grid-template-columns: 110px 1fr 90px;
        align-items: center;
        gap: var(--sp-3);
        padding: var(--sp-2) 0;
        border-bottom: 1px dotted var(--border-soft);
      }
      .row:last-child { border-bottom: none; }
      .row .label {
        color: var(--fg-muted);
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 0.12em;
        text-transform: uppercase;
      }
      .row .value {
        font-family: var(--font-serif);
        font-weight: 350;
        font-size: 22px;
        line-height: 1;
        text-align: right;
        color: var(--fg);
        font-variant-numeric: tabular-nums;
        letter-spacing: -0.01em;
      }
      .row .value small {
        font-family: var(--font-sans);
        font-size: 11px;
        margin-left: 3px;
        color: var(--fg-muted);
        font-weight: 400;
      }
      canvas {
        width: 100%;
        height: 36px;
        display: block;
      }
      .toolbar {
        display: flex;
        gap: var(--sp-1);
        flex-wrap: wrap;
      }
      .chip {
        padding: 4px 12px;
        border-radius: var(--radius-pill);
        background: color-mix(in srgb, var(--surface-2) 50%, transparent);
        border: 1px solid var(--border);
        font-family: var(--font-mono);
        font-size: 11px;
        cursor: pointer;
        color: var(--fg-muted);
        transition: background var(--dur-fast) var(--ease-out),
                    color var(--dur-fast) var(--ease-out),
                    border-color var(--dur-fast) var(--ease-out);
      }
      .chip:hover { color: var(--fg); border-color: var(--border-strong); }
      .chip.active {
        background: color-mix(in srgb, var(--cat-color) 14%, transparent);
        color: var(--cat-color);
        border-color: color-mix(in srgb, var(--cat-color) 50%, transparent);
      }
    `,
  ];

  static properties = {
    frames: { state: true },
    since: { state: true },
    error: { state: true },
    _bands: { state: true },
  };

  constructor() {
    super();
    this.frames = [];
    this.since = '15m';
    this.error = null;
    this._bands = [];       // parsed bias bands from actuator-journal
    this._hoverBand = null; // band under cursor, for tooltip chip
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    this._timer = setInterval(() => this._refresh(), 15000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
  }

  async _refresh() {
    const [tele, journal] = await Promise.all([
      fetchJson(`/api/telemetry/range?since=${this.since}`, { frames: [], error: null }),
      fetchJson('/api/actuator-journal?limit=200', { entries: [] }),
    ]);
    this.frames = tele.frames || [];
    this.error = tele.error || null;
    this._bands = this._parseBands(journal.entries || []);
    this.updateComplete.then(() => this._drawAll());
  }

  /** Parse journal entries into band descriptors. Silently skips malformed entries. */
  _parseBands(entries) {
    const bands = [];
    for (const e of entries) {
      try {
        if (e.kind !== 'apply') continue;
        if (e.error != null) continue;
        if (e.expires_at == null) continue;
        const appliedAt = Number(e.applied_at);
        const expiresAt = Number(e.expires_at);
        if (!isFinite(appliedAt) || !isFinite(expiresAt)) continue;
        if (expiresAt <= appliedAt) continue;
        const intensity = (e.params && e.params.intensity_pct != null)
          ? Number(e.params.intensity_pct) : 10;
        bands.push({
          appliedAt,
          expiresAt,
          intensity: isFinite(intensity) ? intensity : 10,
          actuator: e.actuator || 'unknown',
        });
      } catch (_) {
        // malformed entry — skip silently
      }
    }
    return bands;
  }

  _setSince(s) {
    this.since = s;
    this._refresh();
  }

  _drawAll() {
    const cs = getComputedStyle(document.documentElement);
    const thermal = cs.getPropertyValue('--cat-thermal').trim() || '#ff7373';
    const cooling = cs.getPropertyValue('--cat-cooling').trim() || '#6aa5ff';
    const graph   = cs.getPropertyValue('--cat-graph').trim()   || '#bc8cff';

    // Derive time window from frame timestamps so bias bands align to real wall time.
    const tss = this.frames.map((f) => f.ts).filter((t) => t != null && isFinite(t));
    const windowStart = tss.length ? Math.min(...tss) : 0;
    const windowEnd   = tss.length ? Math.max(...tss) : 0;

    const drawOpts = { windowStart, windowEnd, bands: this._bands };

    // Hot threshold mirrors COOLSTEP_HOT_THRESHOLD_C in daemon.py — visual
    // anchor so the sparkline shows headroom, not just trend.
    this._draw('cpu', this.frames.map((f) => f.cpu_temp), thermal,
               { threshold: 82, thresholdLabel: 'hot 82°', ...drawOpts });
    this._draw('gpu', this.frames.map((f) => f.gpu_temp), cooling, drawOpts);
    this._draw('fan', this.frames.map((f) => f.fan_max_rpm), graph, drawOpts);
  }

  _draw(slot, series, color, opts = {}) {
    const cnv = this.renderRoot?.querySelector(`canvas[data-slot="${slot}"]`);
    if (!cnv) return;
    const data = series.filter((v) => v != null && !isNaN(v));
    const dpr = window.devicePixelRatio || 1;
    const w = cnv.clientWidth;
    const h = cnv.clientHeight;
    cnv.width = w * dpr;
    cnv.height = h * dpr;
    const ctx = cnv.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    // ── Bias overlay bands (drawn FIRST so they sit behind all data lines) ──
    const bands = opts.bands;
    const wStart = opts.windowStart;
    const wEnd   = opts.windowEnd;
    if (bands && bands.length && wEnd > wStart) {
      const wDur = wEnd - wStart;
      for (const band of bands) {
        const xStart = Math.max(0, (band.appliedAt - wStart) / wDur * w);
        const xEnd   = Math.min(w, (Math.min(band.expiresAt, wEnd) - wStart) / wDur * w);
        if (xEnd <= xStart) continue;
        // alpha: intensity 5..20 → 0.2..1.0, floored at 0.2 for legibility
        const alpha = Math.max(0.2, Math.min(1.0, band.intensity / 20));
        ctx.save();
        ctx.globalAlpha = alpha;
        ctx.fillStyle = 'rgba(68, 216, 139, 0.35)';
        ctx.fillRect(xStart, 0, xEnd - xStart, h);
        ctx.restore();
      }
    }

    if (data.length < 2) {
      ctx.fillStyle = '#58627c';
      ctx.font = '11px "Inter", sans-serif';
      ctx.fillText('—', w / 2, h / 2);
      return;
    }
    let min = Math.min(...data);
    let max = Math.max(...data);
    // Force the threshold line into view if it sits just outside actual range
    // so users can see proximity even when the tile is calm.
    if (opts.threshold != null) {
      if (opts.threshold > max && opts.threshold - max < (max - min) * 0.6) max = opts.threshold;
      if (opts.threshold < min && min - opts.threshold < (max - min) * 0.6) min = opts.threshold;
    }
    const range = max - min || 1;

    // Area under the curve — gradient fade from color to transparent
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, color + '55');
    grad.addColorStop(1, color + '00');
    ctx.beginPath();
    ctx.moveTo(0, h);
    data.forEach((v, i) => {
      const x = (i / (data.length - 1)) * w;
      const y = h - ((v - min) / range) * (h - 6) - 3;
      ctx.lineTo(x, y);
    });
    ctx.lineTo(w, h);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // Stroke on top
    ctx.beginPath();
    ctx.lineWidth = 1.6;
    ctx.lineJoin = 'round';
    ctx.strokeStyle = color;
    data.forEach((v, i) => {
      const x = (i / (data.length - 1)) * w;
      const y = h - ((v - min) / range) * (h - 6) - 3;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();

    // Trailing dot at the latest point
    const lastVal = data[data.length - 1];
    const lastX = w;
    const lastY = h - ((lastVal - min) / range) * (h - 6) - 3;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(lastX - 2, lastY, 2.4, 0, Math.PI * 2);
    ctx.fill();

    // Threshold line — dashed, low-contrast; only drawn if the threshold sits
    // inside the visible y-range (we may have widened the range above).
    if (opts.threshold != null && opts.threshold >= min && opts.threshold <= max) {
      const ty = h - ((opts.threshold - min) / range) * (h - 6) - 3;
      ctx.save();
      ctx.strokeStyle = '#f0c44d';
      ctx.globalAlpha = 0.5;
      ctx.setLineDash([3, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, ty);
      ctx.lineTo(w, ty);
      ctx.stroke();
      if (opts.thresholdLabel) {
        ctx.setLineDash([]);
        ctx.globalAlpha = 0.8;
        ctx.fillStyle = '#f0c44d';
        ctx.font = '9px "Inter", sans-serif';
        ctx.fillText(opts.thresholdLabel, 4, Math.max(9, ty - 2));
      }
      ctx.restore();
    }

    // Hover chip: small "bias X% · Ys · actuator" label in top-left corner.
    if (opts.hoverBand) {
      const b = opts.hoverBand;
      const durS = Math.round(b.expiresAt - b.appliedAt);
      const chipText = `bias ${Math.round(b.intensity)}% · ${durS}s · ${b.actuator}`;
      ctx.save();
      ctx.font = 'bold 9px "Inter", sans-serif';
      const tw = ctx.measureText(chipText).width;
      ctx.fillStyle = 'rgba(30, 40, 30, 0.75)';
      ctx.beginPath();
      ctx.roundRect(3, 3, tw + 8, 14, 3);
      ctx.fill();
      ctx.fillStyle = 'rgba(68, 216, 139, 0.95)';
      ctx.fillText(chipText, 7, 13);
      ctx.restore();
    }
  }

  /** Mousemove on any canvas: if cursor X falls inside a bias band, redraw with
   *  a small text chip in the top-left corner of that canvas. */
  _onCanvasMove(e) {
    const cnv = e.currentTarget;
    const slot = cnv.dataset.slot;
    const wStart = this._bands.length && this.frames.length
      ? Math.min(...this.frames.map((f) => f.ts).filter((t) => t != null && isFinite(t)))
      : 0;
    const wEnd = this._bands.length && this.frames.length
      ? Math.max(...this.frames.map((f) => f.ts).filter((t) => t != null && isFinite(t)))
      : 0;
    const wDur = wEnd - wStart;
    if (wDur <= 0 || !this._bands.length) return;

    const rect = cnv.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const w = cnv.clientWidth;
    let hit = null;
    for (const band of this._bands) {
      const xStart = Math.max(0, (band.appliedAt - wStart) / wDur * w);
      const xEnd   = Math.min(w, (Math.min(band.expiresAt, wEnd) - wStart) / wDur * w);
      if (mouseX >= xStart && mouseX <= xEnd) { hit = band; break; }
    }
    if (hit === this._hoverBand) return;
    this._hoverBand = hit;
    // Redraw the affected canvas with the chip overlay.
    this.updateComplete.then(() => {
      const series = slot === 'cpu' ? this.frames.map((f) => f.cpu_temp)
                   : slot === 'gpu' ? this.frames.map((f) => f.gpu_temp)
                   : this.frames.map((f) => f.fan_max_rpm);
      const cs = getComputedStyle(document.documentElement);
      const colors = { cpu: cs.getPropertyValue('--cat-thermal').trim() || '#ff7373',
                       gpu: cs.getPropertyValue('--cat-cooling').trim() || '#6aa5ff',
                       fan: cs.getPropertyValue('--cat-graph').trim()   || '#bc8cff' };
      const extraOpts = slot === 'cpu' ? { threshold: 82, thresholdLabel: 'hot 82°' } : {};
      this._draw(slot, series, colors[slot] || '#aaa',
                 { windowStart: wStart, windowEnd: wEnd, bands: this._bands,
                   hoverBand: hit, ...extraOpts });
    });
  }

  _last(field) {
    for (let i = this.frames.length - 1; i >= 0; i -= 1) {
      const v = this.frames[i]?.[field];
      if (v != null) return v;
    }
    return null;
  }

  _renderRow(label, slot, field, unit, digits) {
    const v = this._last(field);
    return html`
      <div class="row">
        <span class="label">${label}</span>
        <canvas data-slot="${slot}" @mousemove=${this._onCanvasMove}></canvas>
        <span class="value">${fmtNum(v, digits)}<small>${unit}</small></span>
      </div>
    `;
  }

  render() {
    if (this.frames.length === 0) {
      return renderFrame({
        title: 'Trends',
        meta: `last ${this.since}`,
        body: html`<div class="empty">No frames yet — collector fills first samples within seconds of start.</div>`,
      });
    }
    return renderFrame({
      title: 'Trends',
      meta: `${this.frames.length} frames · last ${this.since}`,
      body: html`
        <div class="toolbar">
          ${['5m', '15m', '1h', '6h', '24h'].map(
            (w) => html`<span class="chip ${this.since === w ? 'active' : ''}"
                              @click=${() => this._setSince(w)}>${w}</span>`
          )}
        </div>
        ${this._renderRow('CPU Tctl', 'cpu', 'cpu_temp', '°C', 1)}
        ${this._renderRow('GPU max', 'gpu', 'gpu_temp', '°C', 1)}
        ${this._renderRow('Fan max', 'fan', 'fan_max_rpm', 'RPM', 0)}
        <p class="muted">Plain canvas sparklines · 15s refresh</p>
      `,
    });
  }
}

customElements.define('sparkline-tile', SparklineTile);
