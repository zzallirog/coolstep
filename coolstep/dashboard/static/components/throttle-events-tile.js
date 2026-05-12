import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame } from './_base.js';
import { LangController } from '../i18n/lang-store.js';

export class ThrottleEventsTile extends LitElement {
  static styles = [
    tileBaseStyles,
    css`
      /* Density strip: 168 cells = 7 days × 24 hours */
      .density {
        display: grid;
        grid-template-columns: repeat(24, 1fr);
        gap: 2px;
        margin: 12px 0 6px;
      }
      .density-row {
        display: contents;
      }
      .density-cell {
        height: 8px;
        border-radius: 2px;
        background: var(--surface-2, #1b2437);
        transition: transform 80ms ease;
      }
      .density-cell[data-heat="1"] { background: color-mix(in srgb, var(--cat-color) 32%, var(--surface-2)); }
      .density-cell[data-heat="2"] { background: color-mix(in srgb, var(--cat-color) 55%, var(--surface-2)); }
      .density-cell[data-heat="3"] { background: color-mix(in srgb, var(--cat-color) 78%, var(--surface-2)); }
      .density-cell[data-heat="4"] { background: var(--cat-color); box-shadow: 0 0 6px color-mix(in srgb, var(--cat-color) 60%, transparent); }
      .density-cell:hover { transform: scaleY(1.6); }

      .density-legend {
        display: flex;
        gap: 8px;
        font-size: 10px;
        color: var(--fg-muted);
        margin-bottom: 6px;
        letter-spacing: 0.05em;
        text-transform: uppercase;
      }
      .density-legend .spacer { flex: 1; }
      .density-axis {
        display: flex;
        justify-content: space-between;
        font-size: 9px;
        color: var(--fg-dim);
        font-family: var(--font-mono);
        margin-top: 2px;
      }

      table { width: 100%; font-size: 12px; border-collapse: collapse;
              font-family: var(--font-mono, monospace); }
      th { color: var(--fg-muted); font-weight: 600; text-align: left;
           padding: 6px 8px 6px 0; border-bottom: 1px solid var(--border);
           font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; }
      td { padding: 4px 8px 4px 0; border-bottom: 1px dotted var(--border-soft);
           font-variant-numeric: tabular-nums; }
      td.peak { color: var(--cat-color); font-weight: 500; }
      td.workload { color: var(--fg-muted); }

      details {
        margin-top: 12px;
        border-top: 1px dashed var(--border-soft);
        padding-top: 10px;
      }
      details summary {
        cursor: pointer;
        color: var(--fg-muted);
        font-size: 11px;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        padding: 4px 0;
        list-style: none;
        user-select: none;
      }
      details summary::before {
        content: '▸ ';
        display: inline-block;
        transition: transform 120ms ease;
        color: var(--cat-color);
      }
      details[open] summary::before { content: '▾ '; }
      details summary:hover { color: var(--fg); }
      details .scroll {
        max-height: 320px;
        overflow-y: auto;
        margin-top: 8px;
        scrollbar-width: thin;
      }

      .hot-pill {
        display: inline-flex; align-items: center; gap: 4px;
        padding: 2px 8px; border-radius: 999px;
        background: color-mix(in srgb, var(--cat-color) 18%, transparent);
        color: var(--cat-color);
        font-size: 11px;
        font-variant-numeric: tabular-nums;
        font-weight: 500;
      }
    `,
  ];

  static properties = {
    events: { state: true },
    total: { state: true },
  };

  constructor() {
    super();
    this.events = [];
    this.total = 0;
    this._lang = new LangController(this);
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    this._timer = setInterval(() => this._refresh(), 30000);
    this._sse = new EventSource('/api/sse/telemetry');
    this._sse.onmessage = () => this._refresh();
    this._sse.onerror = () => {};
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
    if (this._sse) this._sse.close();
  }

  async _refresh() {
    const data = await fetchJson('/api/throttle-events?since=7d', { events: [], total: 0 });
    this.events = data.events || [];
    this.total = data.total || 0;
  }

  _formatTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.toISOString().replace('T', ' ').slice(0, 19);
  }

  _formatHHmm(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.toISOString().slice(5, 16).replace('T', ' ');
  }

  /** Build 7×24 grid of {events, peak} bucketed by UTC hour for the last 7d. */
  _buildDensity() {
    const now = Date.now() / 1000;
    const buckets = Array.from({ length: 7 }, () =>
      Array.from({ length: 24 }, () => ({ count: 0, peak: 0 }))
    );
    for (const e of this.events) {
      const ageH = (now - e.ts_start) / 3600;
      if (ageH < 0 || ageH >= 168) continue;
      const d = new Date(e.ts_start * 1000);
      const dayIdx = 6 - Math.floor(ageH / 24); // 0=oldest, 6=today
      const hourIdx = d.getUTCHours();
      if (dayIdx < 0 || dayIdx >= 7) continue;
      const b = buckets[dayIdx][hourIdx];
      b.count += 1;
      if (e.peak_temp > b.peak) b.peak = e.peak_temp;
    }
    return buckets;
  }

  _heatLevel(count) {
    if (count === 0) return 0;
    if (count <= 1) return 1;
    if (count <= 3) return 2;
    if (count <= 6) return 3;
    return 4;
  }

  /** Aggregate: peak°C, top workload, longest event. */
  _summary() {
    let peak = 0;
    let longest = 0;
    const workloadCounts = new Map();
    for (const e of this.events) {
      if (e.peak_temp > peak) peak = e.peak_temp;
      if (e.duration > longest) longest = e.duration;
      const w = e.workload_at_start || '—';
      workloadCounts.set(w, (workloadCounts.get(w) || 0) + 1);
    }
    let topWorkload = '—';
    let topCount = 0;
    for (const [w, c] of workloadCounts) {
      if (w === '—') continue;
      if (c > topCount) { topCount = c; topWorkload = w; }
    }
    return { peak, longest, topWorkload, topCount };
  }

  render() {
    const t = (k, v) => this._lang.t(k, v);
    if (this.events.length === 0) {
      return renderFrame({
        title: t('tile.throttle.title'),
        meta: t('tile.throttle.meta'),
        body: html`
          <div class="hero">
            <div class="col">
              <span class="eyebrow ok">${t('tile.throttle.eyebrow')}</span>
              <span class="metric ok">0</span>
            </div>
          </div>
          <div class="empty">
            ${this.total === 0
              ? t('tile.throttle.empty.noevents')
              : html`${t('tile.throttle.empty.clean')}; <strong>${this.total}</strong>.`}
          </div>
          <p class="muted">${t('common.source')}: <code>/api/throttle-events</code></p>
        `,
      });
    }

    const sum = this._summary();
    const sevCls = this.events.length >= 5 ? 'err' : this.events.length >= 2 ? 'warn' : 'cat';
    const density = this._buildDensity();
    const recent = this.events.slice(0, 5);
    const rest = this.events.slice(5);

    return renderFrame({
      title: t('tile.throttle.title'),
      meta: t('tile.throttle.meta'),
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">${t('tile.throttle.eyebrow')}</span>
            <span class="metric ${sevCls}">${this.events.length}</span>
          </div>
          <div class="aside">
            <span class="eyebrow">${t('tile.throttle.aside.peak')}</span>
            <span class="stat-pill cat">${fmtNum(sum.peak, 1)}°C</span>
            <span class="eyebrow">${t('tile.throttle.aside.longest')}</span>
            <span class="stat-pill">${fmtNum(sum.longest, 0)}s</span>
            <span class="eyebrow">${t('tile.throttle.aside.topwl')}</span>
            <span class="hot-pill" title="${sum.topWorkload} × ${sum.topCount}">${sum.topWorkload === '—' ? '—' : sum.topWorkload}</span>
          </div>
        </div>

        <div class="density-legend">
          <span>${t('tile.throttle.density.label')}</span>
          <span class="spacer"></span>
          <span>${t('tile.throttle.density.cool')}</span>
          <span class="density-cell" data-heat="0" style="width:10px"></span>
          <span class="density-cell" data-heat="1" style="width:10px"></span>
          <span class="density-cell" data-heat="2" style="width:10px"></span>
          <span class="density-cell" data-heat="3" style="width:10px"></span>
          <span class="density-cell" data-heat="4" style="width:10px"></span>
          <span>${t('tile.throttle.density.hot')}</span>
        </div>
        <div class="density">
          ${density.map((row, dayIdx) => html`
            <div class="density-row">
              ${row.map((cell, hourIdx) => {
                const heat = this._heatLevel(cell.count);
                const dayLabel = dayIdx === 6 ? t('tile.throttle.axis.now') : `${6 - dayIdx}d`;
                const title = cell.count > 0
                  ? `${dayLabel} · ${String(hourIdx).padStart(2, '0')}:00 — ${cell.count} ${t('common.events')}, ${cell.peak.toFixed(1)}°C`
                  : `${dayLabel} · ${String(hourIdx).padStart(2, '0')}:00`;
                return html`<span class="density-cell" data-heat="${heat}" title="${title}"></span>`;
              })}
            </div>
          `)}
        </div>
        <div class="density-axis">
          <span>${t('tile.throttle.axis.past')}</span><span>${t('tile.throttle.axis.window')}</span><span>${t('tile.throttle.axis.now')}</span>
        </div>

        <table>
          <thead>
            <tr>
              <th>${t('tile.throttle.col.start')}</th>
              <th>${t('tile.throttle.col.peak')}</th>
              <th>${t('tile.throttle.col.dur')}</th>
              <th>${t('tile.throttle.col.workload')}</th>
            </tr>
          </thead>
          <tbody>
            ${recent.map((e) => html`
              <tr>
                <td>${this._formatHHmm(e.ts_start)}</td>
                <td class="peak">${fmtNum(e.peak_temp, 1)}</td>
                <td>${fmtNum(e.duration, 1)}</td>
                <td class="workload">${e.workload_at_start || '—'}</td>
              </tr>
            `)}
          </tbody>
        </table>

        ${rest.length > 0 ? html`
          <details>
            <summary>${t('tile.throttle.showall', { count: this.events.length })}</summary>
            <div class="scroll">
              <table>
                <thead>
                  <tr>
                    <th>${t('tile.throttle.col.start')}</th>
                    <th>${t('tile.throttle.col.peak')}</th>
                    <th>${t('tile.throttle.col.dur')}</th>
                    <th>${t('tile.throttle.col.cause')}</th>
                    <th>${t('tile.throttle.col.workload')}</th>
                  </tr>
                </thead>
                <tbody>
                  ${rest.map((e) => html`
                    <tr>
                      <td>${this._formatTime(e.ts_start)}</td>
                      <td class="peak">${fmtNum(e.peak_temp, 1)}</td>
                      <td>${fmtNum(e.duration, 1)}</td>
                      <td>${e.cause_label || '—'}</td>
                      <td class="workload">${e.workload_at_start || '—'}</td>
                    </tr>
                  `)}
                </tbody>
              </table>
            </div>
          </details>
        ` : ''}

        <p class="muted" .innerHTML=${t('tile.throttle.foot', { total: this.total })}></p>
      `,
    });
  }
}

customElements.define('throttle-events-tile', ThrottleEventsTile);
