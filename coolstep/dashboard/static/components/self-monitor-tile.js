import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame } from './_base.js';
import { orchestrator } from './_orchestrator.js?v=mount-on-register';

/** Balance-plan step II: <self-monitor-tile>
 *
 * Reads /api/self every 2 s. Renders one mini-cell per known signal id from
 * THRESHOLDS — green when value is below threshold, red when in `triggers[]`.
 * Header meta-chip = micro one-glance plug `cpu N% · ram NM · age Ns`.
 *
 * Signal vocab (matches THRESHOLDS in dashboard/server.py):
 *   busy_ratio_p95   — daemon tick exceeding period budget
 *   slow_tick_count  — >=3 slow ticks per 60-tick window (chronic stall)
 *   memory_used_pct  — cgroup memory.current / memory.max
 *   memory_swap_mb   — any swap = thrash incoming
 *   psi_cpu / mem / io_avg10 — kernel pressure (some) avg10
 *   ml_state_age_sec — staleness of the predictor snapshot file
 */

const SIGNAL_ORDER = [
  'busy_ratio_p95',
  'slow_tick_count',
  'memory_used_pct',
  'memory_swap_mb',
  'psi_cpu_avg10',
  'psi_memory_avg10',
  'psi_io_avg10',
  'ml_state_age_sec',
];

const SIGNAL_LABEL = {
  busy_ratio_p95:   'p95 budget',
  slow_tick_count:  'slow ticks',
  memory_used_pct:  'mem used',
  memory_swap_mb:   'swap',
  psi_cpu_avg10:    'psi cpu',
  psi_memory_avg10: 'psi mem',
  psi_io_avg10:     'psi io',
  ml_state_age_sec: 'ml-state age',
};

const SIGNAL_UNIT = {
  busy_ratio_p95:   '',
  slow_tick_count:  '',
  memory_used_pct:  '%',
  memory_swap_mb:   'M',
  psi_cpu_avg10:    '%',
  psi_memory_avg10: '%',
  psi_io_avg10:     '%',
  ml_state_age_sec: 's',
};

function _signalValue(body, id) {
  if (!body) return null;
  const tick = body.tick || {};
  const mem = body.memory || {};
  const psi = body.psi || {};
  switch (id) {
    case 'busy_ratio_p95':   return tick.busy_ratio_p95_60_ticks;
    case 'slow_tick_count':  return tick.slow_tick_count_60_ticks;
    case 'memory_used_pct':  return mem.used_pct;
    case 'memory_swap_mb':   return mem.swap_mb;
    case 'psi_cpu_avg10':    return psi.cpu_avg10;
    case 'psi_memory_avg10': return psi.memory_avg10;
    case 'psi_io_avg10':     return psi.io_avg10;
    case 'ml_state_age_sec': return body.ml_state_age_sec;
    default: return null;
  }
}

function _fmtSignal(id, v) {
  if (v == null) return '—';
  const unit = SIGNAL_UNIT[id] || '';
  // busy_ratio is a 0-1+ ratio; slow_count is an int; others get 1 decimal.
  if (id === 'slow_tick_count')  return String(v);
  if (id === 'busy_ratio_p95')   return Number(v).toFixed(2);
  return fmtNum(v, 1, unit);
}

export class SelfMonitorTile extends LitElement {
  static get priority() { return 'normal'; }

  static styles = [
    tileBaseStyles,
    css`
      .signal-row {
        display: grid;
        gap: 1px;
        grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
        background: var(--border-soft, rgba(230, 221, 196, 0.05));
        border: 1px solid var(--border-soft, rgba(230, 221, 196, 0.05));
      }
      .signal {
        padding: 8px 10px;
        background: color-mix(in srgb, var(--paper-2, #161c27) 86%, transparent);
        display: flex;
        flex-direction: column;
        gap: 2px;
        position: relative;
        min-width: 0;
      }
      .signal.ok  { box-shadow: inset 2px 0 0 var(--ok, #6fcaa9); }
      .signal.err { box-shadow: inset 2px 0 0 var(--err, #ff6a3d);
                    background: color-mix(in srgb, var(--err, #ff6a3d) 6%, var(--paper-2, #161c27)); }
      .signal.unknown { box-shadow: inset 2px 0 0 var(--fg-dim, #5b6678); opacity: 0.55; }
      .signal .label {
        font-family: var(--font-mono, monospace);
        font-size: 9.5px;
        letter-spacing: var(--track-eyebrow, 0.22em);
        text-transform: uppercase;
        color: var(--fg-muted, #8d9aa9);
        line-height: 1;
      }
      .signal .val {
        font-family: var(--font-mono, monospace);
        font-size: 13px;
        font-variant-numeric: tabular-nums;
        color: var(--fg-strong, #f3ecd4);
        line-height: 1.1;
      }
      .signal.err .val { color: var(--err, #ff6a3d); }
      .signal.ok  .val { color: var(--ok, #6fcaa9); }
      .trigger-journal {
        display: flex;
        flex-direction: column;
        gap: 2px;
        font-family: var(--font-mono, monospace);
        font-size: 11px;
      }
      .trigger-journal .row {
        display: flex;
        justify-content: space-between;
        gap: var(--sp-2, 8px);
        padding: 3px 8px;
        background: color-mix(in srgb, var(--err, #ff6a3d) 8%, transparent);
        border-left: 2px solid var(--err, #ff6a3d);
        color: var(--err, #ff6a3d);
      }
      .trigger-journal .row .id {
        text-transform: uppercase;
        letter-spacing: var(--track-pill, 0.08em);
        font-size: 10px;
      }
      .trigger-journal .row .v {
        font-variant-numeric: tabular-nums;
        opacity: 0.85;
      }
    `,
  ];

  static properties = { report: { state: true } };

  constructor() {
    super();
    this.report = null;
  }

  connectedCallback() {
    super.connectedCallback();
    orchestrator.register('self-monitor-tile', {
      priority: 'lazy',
      element: this,
      mountFn: () => this._mount(),
    });
  }

  _mount() {
    this._refresh();
    // 2s cadence — endpoint is cheap (cached 1s server-side), but PSI/swap
    // signals don't move fast enough to warrant tighter polling.
    this._timer = setInterval(() => this._refresh(), 2000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
  }

  async _refresh() {
    const data = await orchestrator.fetchJson('/api/self', null);
    this.report = data;
  }

  render() {
    const r = this.report;
    if (!r) {
      return renderFrame({
        title: 'Self-monitor',
        meta: '...',
        body: html`<div class="empty">loading self-cost…</div>`,
      });
    }

    const triggerIds = new Set((r.triggers || []).map((t) => t.id));
    const isTriggered = (id) => triggerIds.has(id);
    const valueKnown = (v) => v != null && !Number.isNaN(v);

    // Header meta-chip: micro one-glance line. CPU comes from PSI cpu
    // (system-side pressure), RAM from cgroup memory.current, age from
    // ml-state freshness.
    const cpuPct = r.psi?.cpu_avg10;
    const ramMb = r.memory?.current_mb;
    const ageS = r.ml_state_age_sec;
    const metaParts = [];
    if (valueKnown(cpuPct)) metaParts.push(`psi ${fmtNum(cpuPct, 0)}%`);
    if (valueKnown(ramMb))  metaParts.push(`ram ${fmtNum(ramMb, 0)}M`);
    if (valueKnown(ageS))   metaParts.push(`age ${fmtNum(ageS, 1)}s`);
    const meta = metaParts.join(' · ') || '—';

    const status = r.status || (triggerIds.size > 0 ? 'trigger' : 'ok');
    const heroClass = status === 'trigger' ? 'err' : 'ok';
    const heroText = status === 'trigger'
      ? `${triggerIds.size} TRIGGER`
      : 'OK';

    return renderFrame({
      title: 'Self-monitor',
      meta,
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">observer-effect</span>
            <span class="metric ${heroClass}">${heroText}</span>
          </div>
          <div class="aside">
            <span class="stat-pill ${valueKnown(r.tick?.busy_ratio_ewma) ? '' : ''}">
              busy ${fmtNum(r.tick?.busy_ratio_ewma, 2)}
            </span>
            <span class="stat-pill">
              swap ${fmtNum(r.memory?.swap_mb, 0, 'M')}
            </span>
          </div>
        </div>

        <div class="signal-row">
          ${SIGNAL_ORDER.map((id) => {
            const v = _signalValue(r, id);
            const known = valueKnown(v);
            const cls = !known ? 'unknown' : (isTriggered(id) ? 'err' : 'ok');
            return html`
              <div class="signal ${cls}">
                <span class="label">${SIGNAL_LABEL[id]}</span>
                <span class="val">${_fmtSignal(id, v)}</span>
              </div>
            `;
          })}
        </div>

        ${triggerIds.size > 0
          ? html`
              <h3 class="section-h">Triggered</h3>
              <div class="trigger-journal">
                ${(r.triggers || []).map((t) => html`
                  <div class="row">
                    <span class="id">${t.id}</span>
                    <span class="v">${_fmtSignal(t.id, t.value)} / ${_fmtSignal(t.id, t.threshold)}</span>
                  </div>
                `)}
              </div>
            `
          : ''}

        <p class="muted">
          Source: <code>/api/self</code> · ml-state.json + cgroup PSI + systemctl NRestarts
        </p>
      `,
    });
  }
}

customElements.define('self-monitor-tile', SelfMonitorTile);

export { _signalValue, _fmtSignal, SIGNAL_ORDER };
