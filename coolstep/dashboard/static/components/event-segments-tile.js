import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame, nothing } from './_base.js';
import { orchestrator } from './_orchestrator.js';
import { LangController } from '../i18n/lang-store.js';

/** <event-segments-tile>
 *  Renders recent session/event segment boundaries from
 *  /api/event-segments. A «segment» is a contiguous window of telemetry
 *  framed by a meaningful boundary — workload change, mode flip,
 *  calibration pass, manual snapshot. Orchestrator (Light 2) wires the
 *  endpoint; until then the tile renders the empty state and stays
 *  silent.
 *
 *  Layout mirrors <incidents-tile> intentionally — same hero shape,
 *  same table chrome, same reason-badge column. The simpler «list-only»
 *  body (no expand) is the P2.5 starting form; once the orchestrator
 *  surfaces a session_id → detail route we can lift the expand pattern
 *  from incidents-tile.
 */
export class EventSegmentsTile extends LitElement {
  static get priority() { return 'lazy'; }

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

      td.reason  { color: var(--cat-color); }
      td.session { color: var(--fg-muted); }
      td.dur     { font-weight: 500; }

      .no-segments {
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
    segments: { state: true },
    error: { state: true },
  };

  constructor() {
    super();
    this.segments = [];
    this.error = null;
    this._lc = new LangController(this);
  }

  connectedCallback() {
    super.connectedCallback();
    orchestrator.register('event-segments-tile', {
      priority: 'lazy',
      element: this,
      mountFn: () => this._mount(),
    });
  }

  _mount() {
    this._refresh();
    this._timer = setInterval(() => this._refresh(), 30000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this._timer) clearInterval(this._timer);
  }

  async _refresh() {
    // Non-blocking — if the endpoint isn't wired yet (orchestrator
    // owns that work), fetchJson returns null and we keep the empty
    // state visible. No console noise, no error strip.
    const j = await orchestrator.fetchJson('/api/event-segments?since=24h');
    if (j && Array.isArray(j.segments)) {
      this.segments = j.segments;
      this.error = null;
    } else {
      this.segments = [];
    }
  }

  _formatTs(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.toLocaleString('default', {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  render() {
    const t = (key, fb) => (this._lc.t ? this._lc.t(key, fb) : fb);
    const title = t('tile.events.title', 'Event segments');
    const total = this.segments.length;
    const rows = this.segments.slice(0, 8);

    const head = total > 0
      ? html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow">segments · 24h</span>
            <span class="metric cat">${total}<small>total</small></span>
          </div>
        </div>
      `
      : html`<div class="no-segments">${t('tile.events.empty', 'no segments yet')}</div>`;

    const table = rows.length > 0
      ? html`
        <table>
          <thead>
            <tr>
              <th>ts</th>
              <th>reason</th>
              <th>session</th>
              <th>dur s</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(r => html`
              <tr>
                <td>${this._formatTs(r.ts)}</td>
                <td class="reason">${(r.reason || '').replace(/_/g, ' ')}</td>
                <td class="session">${r.session_id || '—'}</td>
                <td class="dur">${fmtNum(r.duration_s, 1)}</td>
              </tr>
            `)}
          </tbody>
        </table>
      `
      : nothing;

    return renderFrame({
      title,
      meta: total > 0 ? `${total} · 24h` : '24h',
      body: html`${head}${table}`,
    });
  }
}

if (!customElements.get('event-segments-tile')) {
  customElements.define('event-segments-tile', EventSegmentsTile);
}
