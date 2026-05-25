import { LitElement, html, css, fmtNum, tileBaseStyles, fetchJson, renderFrame } from './_base.js';
import { orchestrator } from './_orchestrator.js';

export class LiveTelemetryTile extends LitElement {
  static get priority() { return 'critical'; }
  static styles = [
    tileBaseStyles,
    css`
      :host { grid-column: span 2; }

      .cores {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
        gap: 4px;
      }
      .core {
        display: grid;
        grid-template-columns: auto 1fr auto;
        gap: 6px;
        align-items: center;
        background: color-mix(in srgb, var(--surface-2) 38%, transparent);
        padding: 4px 10px;
        border-radius: 6px;
        font-family: var(--font-mono, monospace);
        font-size: 11px;
        border: 1px solid var(--border-soft);
      }
      .core .id { color: var(--fg-muted); }
      .core .freq { color: var(--cat-color); text-align: right; font-variant-numeric: tabular-nums; }
      /* Parked / boost coloring is about state, not heat. Parked cores idle
       * around 1.1 GHz on Ryzen and shouldn't draw the eye like a boost. */
      .core .freq.parked { color: var(--fg-dim); }
      .core .freq.boost  { color: var(--warn); font-weight: 600; }
      .core-bar {
        position: relative;
        height: 5px;
        background: color-mix(in srgb, var(--bg) 60%, transparent);
        border-radius: 3px;
        overflow: hidden;
      }
      .core-bar .fill {
        position: absolute; left: 0; top: 0; height: 100%;
        background: linear-gradient(90deg,
          color-mix(in srgb, var(--cat-color) 60%, transparent),
          color-mix(in srgb, var(--cat-color) 95%, white));
        transition: width 240ms ease-out;
      }
      .core-bar.hot .fill {
        background: linear-gradient(90deg,
          color-mix(in srgb, var(--warn) 60%, transparent),
          color-mix(in srgb, var(--err) 80%, white));
      }

      table.aux { width: 100%; font-size: 12px; border-collapse: collapse;
                  font-family: var(--font-mono, monospace); }
      table.aux td { padding: 4px 8px 4px 0; border-bottom: 1px dotted var(--border-soft); }
      table.aux td:last-child { text-align: right; color: var(--fg); font-variant-numeric: tabular-nums; }
      table.aux td:first-child { color: var(--fg-muted); }

      .platform-chips { display: flex; gap: 4px; flex-wrap: wrap; }
      .platform-chip {
        padding: 3px 10px;
        border-radius: 999px;
        background: color-mix(in srgb, var(--surface-2) 50%, transparent);
        border: 1px solid var(--border);
        font-family: var(--font-mono, monospace);
        font-size: 11px;
        color: var(--fg-muted);
      }
      .platform-chip strong { color: var(--cat-color); margin-left: 5px; font-weight: 600; }

      /* G-7 — explanatory italic "N/A" for expected platform gaps
       * (Intel iGPU has no discrete power-meter, RAPL CVE-locked, no
       * fan tool, etc).  Silent em-dash looked like daemon-broken in pilot
       * deploy; this surfaces "not applicable here" with hover tooltip. */
      .v.na, .metric.na { color: var(--fg-dim); font-style: italic; font-size: 0.78em; }
    `,
  ];

  static properties = {
    summary: { state: true },     // SSE shortcut row
    raw: { state: true },         // full TelemetryFrame from /api/telemetry/latest
    ageSec: { state: true },
    error: { state: true },
  };

  constructor() {
    super();
    this.summary = null;
    this.raw = null;
    this.ageSec = null;
    this.error = null;
  }

  connectedCallback() {
    super.connectedCallback();
    orchestrator.register('live-telemetry-tile', {
      priority: 'critical',
      element: this,
      mountFn: () => this._mount(),
    });
  }

  _mount() {
    // Subscribe to shared 1Hz telemetry poller instead of own setInterval.
    this._unsubTelemetry = orchestrator.subscribeTelemetry((data) => this._onTelemetry(data));
    this._ageTimer = setInterval(() => {
      if (this.summary?.ts) this.ageSec = Math.round(Date.now() / 1000 - this.summary.ts);
    }, 1000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this._unsubTelemetry) this._unsubTelemetry();
    clearInterval(this._ageTimer);
  }

  _onTelemetry(data) {
    if (this.summary?.ts !== data.ts) {
      this.summary = data;
      this.ageSec = 0;
    }
    if (data.raw) {
      this.raw = data.raw;
      this.error = null;
    }
  }

  // Keep fetchJson wrapper for compatibility
  async _refreshAll() {
    const data = await orchestrator.fetchJson('/api/telemetry/latest', null, { priority: 'critical' });
    if (!data) return;
    this._onTelemetry(data);
  }

  _renderCores() {
    const r = this.raw;
    if (!r?.cpu?.freq_mhz?.length) return '';
    const freqs = r.cpu.freq_mhz;
    const loads = r.cpu.load_pct || [];
    return html`
      <div class="cores">
        ${freqs.map(
          (f, i) => {
            const load = loads[i] ?? 0;
            // <2 GHz = parked / deep idle, >5 GHz = boosted past base clock.
            const freqCls = f < 2000 ? 'parked' : f > 5000 ? 'boost' : '';
            return html`
              <div class="core">
                <span class="id">c${i}</span>
                <div class="core-bar ${load > 80 ? 'hot' : ''}">
                  <div class="fill" style="width: ${Math.min(100, load)}%"></div>
                </div>
                <span class="freq ${freqCls}">${Math.round(f)}</span>
              </div>
            `;
          }
        )}
      </div>
    `;
  }

  _renderGpus() {
    const gpus = this.raw?.gpus || [];
    if (!gpus.length) return '';
    return html`
      <table class="aux">
        ${gpus.map(
          (g) => html`
            <tr>
              <td>${g.name}</td>
              <td>${fmtNum(g.temp_c, 1, '°C')} ·
                  ${fmtNum(g.power_w, 1, 'W')} ·
                  ${fmtNum(g.sclk_mhz, 0, ' MHz')}</td>
            </tr>
          `
        )}
      </table>
    `;
  }

  _renderTempsBlock(label, dict) {
    if (!dict || !Object.keys(dict).length) return '';
    return html`
      <table class="aux">
        ${Object.entries(dict).map(
          ([k, v]) => html`<tr><td>${k}</td><td>${fmtNum(v, 1, '°C')}</td></tr>`
        )}
      </table>
    `;
  }

  _renderPlatform() {
    const ps = this.raw?.platform_state || {};
    if (!Object.keys(ps).length) return '';
    return html`
      <div class="platform-chips">
        ${Object.entries(ps).map(
          ([k, v]) => html`<span class="platform-chip">${k}<strong>${v}</strong></span>`
        )}
      </div>
    `;
  }

  render() {
    const f = this.summary || {};
    if (!this.summary && !this.raw) {
      return renderFrame({
        title: 'Live telemetry',
        body: html`<div class="empty">Awaiting first frame from collector…</div>`,
      });
    }
    const meta = this.ageSec != null
      ? html`<span class=${this.ageSec > 5 ? 'stale' : ''}>age ${this.ageSec}s</span>`
      : 'connecting…';
    const tctl = f.cpu_temp;
    const tempCls = tctl == null ? 'na' : tctl >= 90 ? 'err' : tctl >= 80 ? 'warn' : tctl >= 65 ? 'cat' : 'ok';

    // G-7 — per-signal reason rendered into tooltip when value is unavailable
    // for this platform (not «pending tick», but «structurally absent»).
    // Backlog: extend collector schema with `unavailable_reason` so backend
    // owns the explanation; for now these strings are stable platform-derived.
    const naReasons = {
      cpu_temp: 'CPU package temp not exposed by this platform',
      gpu_temp: 'no discrete GPU temp sensor (Intel iGPU shares CPU package)',
      cpu_power: 'RAPL energy_uj root-only (CVE-2020-8694)',
      gpu_power: 'GPU power needs nvidia-smi / amdgpu hwmon / intel_gpu_top -J root',
      fan_max_rpm: 'no fan-control tool (asusctl / nbfc / thinkfan / fancontrol)',
    };
    const valOrNA = (key, value, digits, suffix) => (
      value != null && !isNaN(value)
        ? html`${fmtNum(value, digits)}<small>${suffix}</small>`
        : html`<span class="na" title=${naReasons[key] || 'unavailable on this platform'}>N/A</span>`
    );

    return renderFrame({
      title: 'Live telemetry',
      meta,
      body: html`
        ${this.error ? html`<div class="error-strip">${this.error}</div>` : ''}

        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">CPU Tctl</span>
            <span class="metric ${tempCls}" title=${tctl == null ? naReasons.cpu_temp : ''}>
              ${tctl != null ? html`${fmtNum(tctl, 1)}<small>°C</small>` : html`N/A`}
            </span>
          </div>
          <div class="aside">
            <span class="eyebrow">workload</span>
            <span class="stat-pill cat">${f.workload_label || 'uncategorised'}</span>
          </div>
        </div>

        <div class="mini-grid">
          <div class="mini">
            <span class="eyebrow">GPU max</span>
            <span class="v ${f.gpu_temp == null ? 'na' : ''}">${valOrNA('gpu_temp', f.gpu_temp, 1, '°C')}</span>
          </div>
          <div class="mini">
            <span class="eyebrow">CPU power</span>
            <span class="v ${f.cpu_power == null ? 'na' : ''}">${valOrNA('cpu_power', f.cpu_power, 1, 'W')}</span>
          </div>
          <div class="mini">
            <span class="eyebrow">GPU power</span>
            <span class="v ${f.gpu_power == null ? 'na' : ''}">${valOrNA('gpu_power', f.gpu_power, 1, 'W')}</span>
          </div>
          <div class="mini">
            <span class="eyebrow">Fan max</span>
            <span class="v ${f.fan_max_rpm == null ? 'na' : ''}">${valOrNA('fan_max_rpm', f.fan_max_rpm, 0, 'rpm')}</span>
          </div>
        </div>

        <div>
          <h3 class="section-h">Per-core</h3>
          ${this._renderCores()}
        </div>

        ${this.raw?.gpus?.length ? html`
          <div>
            <h3 class="section-h">GPUs</h3>
            ${this._renderGpus()}
          </div>` : ''}

        ${this.raw?.storage_temps_c && Object.keys(this.raw.storage_temps_c).length ? html`
          <div>
            <h3 class="section-h">Storage temps</h3>
            ${this._renderTempsBlock('storage', this.raw.storage_temps_c)}
          </div>` : ''}

        ${this.raw?.memory_temps_c && Object.keys(this.raw.memory_temps_c).length ? html`
          <div>
            <h3 class="section-h">Memory temps</h3>
            ${this._renderTempsBlock('memory', this.raw.memory_temps_c)}
          </div>` : ''}

        ${this.raw?.platform_state && Object.keys(this.raw.platform_state).length ? html`
          <div>
            <h3 class="section-h">Platform state</h3>
            ${this._renderPlatform()}
          </div>` : ''}

        <p class="muted">Source: SSE · raw via /api/telemetry/latest</p>
      `,
    });
  }
}

customElements.define('live-telemetry-tile', LiveTelemetryTile);
