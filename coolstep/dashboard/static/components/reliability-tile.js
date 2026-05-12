import { LitElement, html, css, fetchJson, tileBaseStyles, renderFrame } from './_base.js';

/** Daemon reliability surface.
 *
 * Reads /api/reliability every 30 s. Four numbers:
 *   uptime_sec    — since current process started (systemctl monotonic)
 *   restart_count — NRestarts from systemd unit
 *   last_crash    — most recent hard-crash recovery event (or null = "never")
 *   mtbf_sec      — rough estimate; equals uptime when no crashes ever recorded
 *
 * Every value tolerates null — the tile renders «—» / «never» rather than
 * breaking when systemctl is unavailable (CI, non-systemd hosts).
 */

function _fmtDuration(sec) {
  if (sec == null || isNaN(sec) || sec < 0) return '—';
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function _t(host, key, fallback) {
  // Defensive i18n lookup — lang-store may not be ready when tile mounts.
  try {
    const store = window.__coolstepLangStore;
    if (store && typeof store.t === 'function') return store.t(key, fallback);
  } catch (_e) { /* fall through */ }
  return fallback;
}

export class ReliabilityTile extends LitElement {
  static styles = [
    tileBaseStyles,
    css`
      .warn-banner {
        padding: var(--sp-2, 8px) var(--sp-3, 12px);
        border-radius: var(--radius-sm, 6px);
        background: var(--warn-soft, rgba(240, 196, 77, 0.14));
        border-left: 3px solid var(--warn, #f0c44d);
        color: var(--warn, #f0c44d);
        font-size: var(--font-size-sm, 13px);
      }
    `,
  ];

  static properties = {
    report: { state: true },
  };

  constructor() {
    super();
    this.report = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    this._timer = setInterval(() => this._refresh(), 30000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
  }

  async _refresh() {
    const data = await fetchJson(
      '/api/reliability',
      { uptime_sec: null, restart_count: null, last_crash: null, mtbf_sec: null },
    );
    this.report = data;
  }

  render() {
    const r = this.report || {
      uptime_sec: null, restart_count: null, last_crash: null, mtbf_sec: null,
    };

    const titleStr     = _t(this, 'tile.reliability.title',      'Reliability');
    const uptimeLbl    = _t(this, 'tile.reliability.uptime',     'uptime');
    const restartsLbl  = _t(this, 'tile.reliability.restarts',   'restarts');
    const lastCrashLbl = _t(this, 'tile.reliability.last_crash', 'last crash');
    const mtbfLbl      = _t(this, 'tile.reliability.mtbf',       'MTBF est');
    const neverStr     = _t(this, 'tile.reliability.never',      'never');
    const warnTpl      = _t(this, 'tile.reliability.crash_warn', 'recovered {age} ago');

    const uptimeFmt = _fmtDuration(r.uptime_sec);
    const restartsFmt = r.restart_count == null ? '—' : String(r.restart_count);
    const lastCrashAge = r.last_crash && r.last_crash.age_sec != null
      ? r.last_crash.age_sec : null;
    const lastCrashFmt = lastCrashAge == null ? neverStr : _fmtDuration(lastCrashAge);
    const mtbfFmt = _fmtDuration(r.mtbf_sec);

    const isRecentCrash = lastCrashAge != null && lastCrashAge < 86400;
    const restartsCls = (r.restart_count != null && r.restart_count > 0) ? 'warn' : 'ok';

    return renderFrame({
      title: titleStr,
      meta: r.uptime_sec != null ? uptimeFmt : '—',
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">${uptimeLbl}</span>
            <span class="metric cat">${uptimeFmt}</span>
          </div>
        </div>

        <div class="mini-grid">
          <div class="mini ${restartsCls === 'warn' ? 'warn' : 'ok'}">
            <span class="eyebrow">${restartsLbl}</span>
            <span class="v ${restartsCls}">${restartsFmt}</span>
          </div>
          <div class="mini ${isRecentCrash ? 'warn' : ''}">
            <span class="eyebrow">${lastCrashLbl}</span>
            <span class="v ${isRecentCrash ? 'warn' : ''}">${lastCrashFmt}</span>
          </div>
          <div class="mini">
            <span class="eyebrow">${mtbfLbl}</span>
            <span class="v">${mtbfFmt}</span>
          </div>
        </div>

        ${isRecentCrash
          ? html`<div class="warn-banner">
              ⚠ ${warnTpl.replace('{age}', _fmtDuration(lastCrashAge))}
              ${r.last_crash?.kind ? html` <small>(${r.last_crash.kind})</small>` : ''}
            </div>`
          : ''}

        <p class="muted">
          Source: <code>/api/reliability</code> · systemctl NRestarts + monotonic uptime ·
          last-crash-recovery.json
        </p>
      `,
    });
  }
}

customElements.define('reliability-tile', ReliabilityTile);

// Export for tests
export { _fmtDuration };
