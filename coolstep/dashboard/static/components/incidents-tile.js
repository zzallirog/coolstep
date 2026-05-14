import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame, nothing } from './_base.js';
import { LangController } from '../i18n/lang-store.js';

/** <incidents-tile>
 *  Renders the rolling 7-day incident log: every quiet_safety_eject
 *  + throttle_close emits a row in data/incidents.jsonl with a
 *  multi-angle similarity verdict against prior incidents.
 *
 *  Layout:
 *   - hero: count in last 7d  +  last-eject age
 *   - table: last 5 incidents (ts · kind · peak · workload · top match)
 *   - row click → expanded multi-angle similarity table fetched live
 *     from /api/incidents/<ts>/similar
 *
 *  The expanded table groups matches by angle and renders the `why`
 *  string for each — that's where the user can read «cosine=0.94 on
 *  26-dim telemetry embedding» or «same weekday + hour-of-day».
 */
export class IncidentsTile extends LitElement {
  static styles = [
    tileBaseStyles,
    css`
      :host { grid-column: span 2; }

      table { width: 100%; font-size: 12px; border-collapse: collapse;
              font-family: var(--font-mono); }
      th { color: var(--fg-muted); font-weight: 500; text-align: left;
           padding: 6px 8px 6px 0; border-bottom: 1px solid var(--border);
           font-size: 10px; letter-spacing: var(--track-pill, 0.08em);
           text-transform: uppercase; }
      td { padding: 5px 8px 5px 0; border-bottom: 1px dotted var(--border-soft);
           font-variant-numeric: tabular-nums; }
      tr.row { cursor: pointer; transition: background var(--dur-fast) var(--ease-out); }
      tr.row:hover { background: color-mix(in srgb, var(--cat-color) 6%, transparent); }
      tr.row.open { background: color-mix(in srgb, var(--cat-color) 10%, transparent); }

      td.kind-quiet_eject  { color: var(--cat-graph); }
      td.kind-throttle_close { color: var(--cat-thermal); }
      td.peak  { font-weight: 500; }
      td.workload { color: var(--fg-muted); }

      .angles {
        background: color-mix(in srgb, var(--paper-2) 70%, transparent);
        padding: 10px 12px;
        border-left: 2px solid var(--cat-color);
        margin: 4px 0 8px 0;
      }
      .angle-block { margin-bottom: 8px; }
      .angle-block:last-child { margin-bottom: 0; }
      .angle-name {
        font-family: var(--font-mono);
        font-size: 10px;
        letter-spacing: var(--track-eyebrow, 0.22em);
        text-transform: uppercase;
        color: var(--cat-color);
        padding-bottom: 3px;
        border-bottom: 1px dashed var(--border-soft);
        margin-bottom: 5px;
      }
      .angle-match {
        display: grid;
        grid-template-columns: 110px 1fr 60px;
        gap: 8px;
        font-size: 11px;
        line-height: 1.5;
        color: var(--fg);
        padding: 2px 0;
      }
      .angle-match .ts { color: var(--fg-muted); }
      .angle-match .why { color: var(--fg); }
      .angle-match .dist { color: var(--fg-muted); text-align: right; font-variant-numeric: tabular-nums; }

      .no-incidents {
        text-align: center;
        padding: 24px 12px;
        color: var(--fg-muted);
        font-family: var(--font-mono);
        letter-spacing: var(--track-pill);
        text-transform: uppercase;
        font-size: 10.5px;
      }
    `,
  ];

  static properties = {
    incidents: { state: true },
    selectedTs: { state: true },
    angleMatches: { state: true },
    error: { state: true },
  };

  constructor() {
    super();
    this.incidents = [];
    this.selectedTs = null;
    this.angleMatches = null;
    this.error = null;
    this._lc = new LangController(this);
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    this._timer = setInterval(() => this._refresh(), 30000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this._timer) clearInterval(this._timer);
  }

  async _refresh() {
    try {
      const j = await fetchJson('/api/incidents?since=7d&limit=50');
      if (j && Array.isArray(j.incidents)) {
        this.incidents = j.incidents;
        this.error = null;
      }
    } catch (_e) {
      this.error = String(_e);
    }
  }

  async _toggleRow(ts) {
    if (this.selectedTs === ts) {
      this.selectedTs = null;
      this.angleMatches = null;
      return;
    }
    this.selectedTs = ts;
    this.angleMatches = null;
    try {
      const j = await fetchJson(`/api/incidents/${ts}/similar`);
      this.angleMatches = (j && Array.isArray(j.similar)) ? j.similar : [];
    } catch (_e) {
      this.angleMatches = [];
    }
  }

  _formatTs(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.toLocaleString('default', {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  _lastEjectAge() {
    const ejects = this.incidents.filter(i => i.kind === 'quiet_eject');
    if (!ejects.length) return null;
    const last = ejects[0].ts;
    const ageSec = Date.now() / 1000 - last;
    if (ageSec < 60) return `${Math.round(ageSec)}s`;
    if (ageSec < 3600) return `${Math.round(ageSec / 60)}m`;
    if (ageSec < 86400) return `${Math.round(ageSec / 3600)}h`;
    return `${Math.round(ageSec / 86400)}d`;
  }

  _groupByAngle(matches) {
    const groups = new Map();
    for (const m of matches || []) {
      const arr = groups.get(m.angle) || [];
      arr.push(m);
      groups.set(m.angle, arr);
    }
    return groups;
  }

  _renderAngles(matches) {
    if (!matches) return html`<div class="empty">loading…</div>`;
    if (!matches.length) return html`<div class="empty">no similar incidents</div>`;
    const groups = this._groupByAngle(matches);
    return html`
      <div class="angles">
        ${[...groups.entries()].map(([angle, rows]) => html`
          <div class="angle-block">
            <div class="angle-name">${angle.replace(/_/g, ' ')}</div>
            ${rows.map(m => html`
              <div class="angle-match">
                <span class="ts">${this._formatTs(m.ts)}</span>
                <span class="why">${m.why || ''}</span>
                <span class="dist">${m.distance?.toFixed(2)}</span>
              </div>
            `)}
          </div>
        `)}
      </div>
    `;
  }

  render() {
    const total = this.incidents.length;
    const lastEject = this._lastEjectAge();
    const ejects = this.incidents.filter(i => i.kind === 'quiet_eject').length;
    const throttles = this.incidents.filter(i => i.kind === 'throttle_close').length;
    const head = total > 0
      ? html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow">incidents · 7d</span>
            <span class="metric cat">${total}<small>total</small></span>
          </div>
          <div class="col">
            <span class="eyebrow">quiet ejects</span>
            <span class="metric">${ejects}</span>
          </div>
          <div class="col">
            <span class="eyebrow">throttles</span>
            <span class="metric">${throttles}</span>
          </div>
          <div class="aside">
            ${lastEject ? html`
              <span class="eyebrow">last eject</span>
              <span class="stat-pill cat">${lastEject} ago</span>
            ` : html`<span class="muted">no recent ejects</span>`}
          </div>
        </div>
      `
      : html`<div class="no-incidents">no incidents in last 7 days</div>`;

    const rows = this.incidents.slice(0, 8);
    const table = rows.length > 0
      ? html`
        <table>
          <thead>
            <tr>
              <th>ts</th>
              <th>kind</th>
              <th>peak °C</th>
              <th>dur s</th>
              <th>workload</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(r => html`
              <tr class="row ${this.selectedTs === r.ts ? 'open' : ''}"
                  @click=${() => this._toggleRow(r.ts)}>
                <td>${this._formatTs(r.ts)}</td>
                <td class="kind-${r.kind}">${(r.kind || '').replace(/_/g, ' ')}</td>
                <td class="peak">${fmtNum(r.peak_temp_c, 1)}</td>
                <td>${fmtNum(r.duration_s, 1)}</td>
                <td class="workload">${r.workload_class || '—'}</td>
              </tr>
              ${this.selectedTs === r.ts
                ? html`<tr><td colspan="5">${this._renderAngles(this.angleMatches)}</td></tr>`
                : nothing}
            `)}
          </tbody>
        </table>
      `
      : nothing;

    return renderFrame({
      title: 'Incidents',
      meta: total > 0 ? `${total} · 7d` : '7d',
      body: html`${head}${table}`,
    });
  }
}

if (!customElements.get('incidents-tile')) {
  customElements.define('incidents-tile', IncidentsTile);
}
