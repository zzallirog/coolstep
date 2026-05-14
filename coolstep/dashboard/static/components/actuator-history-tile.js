import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame } from './_base.js';
import { LangController } from '../i18n/lang-store.js';

const LS_KEY = 'coolstep-actuator-filter';

function _lsGet() {
  try { return localStorage.getItem(LS_KEY) || 'all'; } catch { return 'all'; }
}
function _lsSet(v) {
  try { localStorage.setItem(LS_KEY, v); } catch {}
}

export class ActuatorHistoryTile extends LitElement {
  static styles = [
    tileBaseStyles,
    css`
      /* ---- filter pills bar ---- */
      .filter-bar {
        display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap;
      }
      .filter-btn {
        display: inline-flex; align-items: center;
        padding: 2px 10px; border-radius: 999px;
        font-size: 10px; font-weight: 600;
        letter-spacing: 0.06em; text-transform: uppercase;
        font-family: var(--font-mono, monospace);
        cursor: pointer; border: 1px solid var(--border);
        background: transparent; color: var(--fg-muted);
        transition: background 0.12s, color 0.12s, border-color 0.12s;
      }
      .filter-btn:hover { color: var(--fg); border-color: var(--fg-muted); }
      .filter-btn.active {
        background: color-mix(in srgb, var(--cat-color) 18%, transparent);
        color: var(--cat-color);
        border-color: color-mix(in srgb, var(--cat-color) 42%, transparent);
      }

      /* ---- kind pill (inline in table) ---- */
      .kind-pill {
        display: inline-flex; align-items: center;
        padding: 1px 7px; border-radius: 999px;
        font-size: 10px; font-weight: 600;
        letter-spacing: 0.06em; text-transform: uppercase;
        font-family: var(--font-mono, monospace);
        background: color-mix(in srgb, var(--fg-dim) 18%, transparent);
        color: var(--fg-muted);
        border: 1px solid color-mix(in srgb, var(--fg-dim) 32%, transparent);
      }
      .kind-pill.apply-ok {
        background: color-mix(in srgb, var(--cat-color) 18%, transparent);
        color: var(--cat-color);
        border-color: color-mix(in srgb, var(--cat-color) 42%, transparent);
      }
      .kind-pill.apply-err {
        background: var(--err-soft, rgba(255,115,115,0.14));
        color: var(--err, #ff7373);
        border-color: color-mix(in srgb, var(--err, #ff7373) 42%, transparent);
      }
      .kind-pill.revert-ok {
        background: var(--ok-soft, rgba(68,216,139,0.14));
        color: var(--ok, #44d88b);
        border-color: color-mix(in srgb, var(--ok, #44d88b) 42%, transparent);
      }
      .kind-pill.revert-err {
        background: var(--warn-soft, rgba(240,196,77,0.14));
        color: var(--warn, #f0c44d);
        border-color: color-mix(in srgb, var(--warn, #f0c44d) 42%, transparent);
      }

      /* ---- table ---- */
      table { width: 100%; font-size: 12px; border-collapse: collapse;
              font-family: var(--font-mono, monospace); }
      th { color: var(--fg-muted); font-weight: 600; text-align: left;
           padding: 6px 8px 6px 0; border-bottom: 1px solid var(--border);
           font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; }
      td { padding: 4px 8px 4px 0; border-bottom: 1px dotted var(--border-soft);
           font-variant-numeric: tabular-nums; vertical-align: top; }
      td.actuator { color: var(--fg-muted); white-space: nowrap; }
      td.cmd {
        color: var(--fg-dim, #58627c); font-size: 11px;
        max-width: 260px; overflow: hidden;
        text-overflow: ellipsis; white-space: nowrap; cursor: help;
      }
      td.cmd[data-empty="1"] { color: var(--fg-dim); font-style: italic; }
      td.ttl { color: var(--fg-muted); white-space: nowrap; }
      td.ttl.live { color: var(--ok, #44d88b); }
      td.ttl.expired { color: var(--fg-dim); }

      /* ---- mode pill (legacy, kept for possible reuse) ---- */
      .mode-pill {
        display: inline-flex; align-items: center;
        padding: 1px 7px; border-radius: 999px;
        font-size: 10px; font-weight: 600;
        letter-spacing: 0.06em; text-transform: uppercase;
        font-family: var(--font-mono, monospace);
        background: color-mix(in srgb, var(--fg-dim) 18%, transparent);
        color: var(--fg-muted);
        border: 1px solid color-mix(in srgb, var(--fg-dim) 32%, transparent);
      }
      .mode-pill.dry {
        background: color-mix(in srgb, var(--fg-muted) 14%, transparent);
        color: var(--fg-muted);
        border-color: color-mix(in srgb, var(--fg-muted) 32%, transparent);
      }
      .mode-pill.armed {
        background: color-mix(in srgb, var(--cat-color) 18%, transparent);
        color: var(--cat-color);
        border-color: color-mix(in srgb, var(--cat-color) 42%, transparent);
      }
      .mode-pill.error {
        background: var(--err-soft, rgba(255,115,115,0.14));
        color: var(--err, #ff7373);
        border-color: color-mix(in srgb, var(--err, #ff7373) 42%, transparent);
      }

      details {
        margin-top: 12px; border-top: 1px dashed var(--border-soft); padding-top: 10px;
      }
      details summary {
        cursor: pointer; color: var(--fg-muted); font-size: 11px;
        letter-spacing: 0.06em; text-transform: uppercase;
        padding: 4px 0; list-style: none; user-select: none;
      }
      details summary::before { content: '▸ '; display: inline-block; color: var(--cat-color); }
      details[open] summary::before { content: '▾ '; }
      details summary:hover { color: var(--fg); }
      details .scroll {
        max-height: 320px; overflow-y: auto; margin-top: 8px; scrollbar-width: thin;
      }
    `,
  ];

  static properties = {
    entries: { state: true },
    total: { state: true },
    _now: { state: true },
    _filter: { state: true },
  };

  constructor() {
    super();
    this.entries = [];
    this.total = 0;
    this._now = Date.now() / 1000;
    this._filter = _lsGet();
    this._lang = new LangController(this);
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    this._timer = setInterval(() => this._refresh(), 30000);
    this._tick = setInterval(() => this._tickCountdown(), 1000);
    // Balance-plan step IV: SSE replaced by 1Hz dedup poll. SSE was only
    // a "fresh telemetry — refresh me" trigger; one polled fetch matches.
    this._sseLastTs = null;
    this._ssePollTimer = setInterval(async () => {
      const data = await fetchJson('/api/telemetry/latest', null);
      if (data && data.ts !== this._sseLastTs) {
        this._sseLastTs = data.ts;
        this._refresh();
      }
    }, 1000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
    clearInterval(this._tick);
    clearInterval(this._ssePollTimer);
  }

  _tickCountdown() {
    this._now = Date.now() / 1000;
    this.requestUpdate();
  }

  async _refresh() {
    const data = await fetchJson('/api/actuator-journal?limit=50', { entries: [], total: 0 });
    this.entries = data.entries || [];
    this.total = data.total || 0;
    this._now = Date.now() / 1000;
  }

  _setFilter(f) {
    this._filter = f;
    _lsSet(f);
  }

  _filteredEntries() {
    const f = this._filter;
    if (f === 'all') return this.entries;
    if (f === 'apply') return this.entries.filter((e) => e.kind === 'apply' && !e.error);
    if (f === 'revert') return this.entries.filter((e) => e.kind === 'revert');
    if (f === 'error') return this.entries.filter((e) => !!e.error);
    return this.entries;
  }

  _fmtTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.toISOString().replace('T', ' ').slice(5, 19);
  }

  _fmtAge(ts) {
    if (!ts) return '—';
    const age = Math.max(0, this._now - ts);
    if (age < 60) return `${Math.round(age)}s`;
    if (age < 3600) return `${Math.round(age / 60)}m`;
    if (age < 86400) return `${Math.round(age / 3600)}h`;
    return `${Math.round(age / 86400)}d`;
  }

  // Returns timestamp to use as "when" for an entry (apply_at or ts).
  _entryTs(entry) {
    return entry.applied_at || entry.ts || null;
  }

  _kindPill(entry) {
    const kind = entry.kind || 'apply';
    const hasErr = !!entry.error;

    if (kind === 'revert') {
      const cls = hasErr ? 'revert-err' : 'revert-ok';
      return html`<span class="kind-pill ${cls}">revert</span>`;
    }
    // apply
    const cls = hasErr ? 'apply-err' : 'apply-ok';
    const label = hasErr ? 'error' : 'apply';
    return html`<span class="kind-pill ${cls}">${label}</span>`;
  }

  _ttlCell(entry) {
    const t = (k, v) => this._lang.t(k, v);
    if (entry.kind === 'revert') return html`<td class="ttl">—</td>`;
    const expiresAt = entry.expires_at;
    if (expiresAt == null) return html`<td class="ttl">—</td>`;
    const remaining = expiresAt - this._now;
    if (remaining > 0) {
      return html`<td class="ttl live">${t('tile.actuator.ttl.live', { sec: Math.round(remaining) })}</td>`;
    }
    const age = Math.round(this._now - expiresAt);
    return html`<td class="ttl expired">${t('tile.actuator.ttl.expired', { age })}</td>`;
  }

  _cmdCell(entry) {
    const cmd = entry.cmd_executed;
    if (!cmd) {
      const tail = entry.stdout_tail || '';
      if (!tail) return html`<td class="cmd" data-empty="1">—</td>`;
      const short = tail.length > 70 ? tail.slice(0, 70) + '…' : tail;
      return html`<td class="cmd" title="${tail}">${short}</td>`;
    }
    const short = cmd.length > 70 ? cmd.slice(0, 70) + '…' : cmd;
    return html`<td class="cmd" title="${cmd}">${short}</td>`;
  }

  // Armed = apply entries with expires_at > now not yet superseded by a later revert.
  _armedCount() {
    // Build set of actuators that have a revert after their latest apply.
    const latestRevertTs = {};
    for (const e of this.entries) {
      if (e.kind === 'revert' && e.actuator) {
        const ts = e.ts || 0;
        if ((latestRevertTs[e.actuator] || 0) < ts) latestRevertTs[e.actuator] = ts;
      }
    }
    let n = 0;
    for (const e of this.entries) {
      if (e.kind !== 'apply' || e.error || e.expires_at == null) continue;
      if (e.expires_at <= this._now) continue;
      const applyTs = e.applied_at || e.ts || 0;
      const revertTs = latestRevertTs[e.actuator] || 0;
      if (revertTs > applyTs) continue;
      n += 1;
    }
    return n;
  }

  _lastApplyAge() {
    for (const e of this.entries) {
      if (e.kind === 'apply' || !e.kind) {
        const ts = e.applied_at || e.ts;
        if (ts) return Math.max(0, this._now - ts);
      }
    }
    return null;
  }

  _renderFilterBar(t) {
    const filters = [
      { key: 'all',    label: t('tile.actuator.filter.all') },
      { key: 'apply',  label: t('tile.actuator.filter.apply') },
      { key: 'revert', label: t('tile.actuator.filter.revert') },
      { key: 'error',  label: t('tile.actuator.filter.error') },
    ];
    return html`
      <div class="filter-bar">
        ${filters.map((f) => html`
          <button
            class="filter-btn ${this._filter === f.key ? 'active' : ''}"
            @click=${() => this._setFilter(f.key)}
          >${f.label}</button>
        `)}
      </div>
    `;
  }

  _renderTable(rows, t) {
    return html`
      <table>
        <thead>
          <tr>
            <th>${t('tile.actuator.col.ts')}</th>
            <th>${t('tile.actuator.col.kind')}</th>
            <th>${t('tile.actuator.col.actuator')}</th>
            <th>${t('tile.actuator.col.cmd')}</th>
            <th>${t('tile.actuator.col.ttl')}</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((e) => html`
            <tr>
              <td>${this._fmtTime(this._entryTs(e))}</td>
              <td>${this._kindPill(e)}</td>
              <td class="actuator">${e.actuator || '—'}</td>
              ${this._cmdCell(e)}
              ${this._ttlCell(e)}
            </tr>
          `)}
        </tbody>
      </table>
    `;
  }

  render() {
    const t = (k, v) => this._lang.t(k, v);
    const filtered = this._filteredEntries();

    if (this.entries.length === 0) {
      return renderFrame({
        title: t('tile.actuator.title'),
        meta: t('tile.actuator.meta'),
        body: html`
          <div class="hero">
            <div class="col">
              <span class="eyebrow">${t('tile.actuator.hero.entries')}</span>
              <span class="metric">0</span>
            </div>
          </div>
          ${this._renderFilterBar(t)}
          <div class="empty">${t('tile.actuator.empty')}</div>
          <p class="muted">${t('common.source')}: <code>/api/actuator-journal</code></p>
        `,
      });
    }

    const armed = this._armedCount();
    const lastAge = this._lastApplyAge();
    const lastAgeLabel = lastAge == null
      ? t('tile.actuator.idle')
      : t('tile.actuator.lastapply', { age: this._fmtAge(this._entryTs(this.entries[0])) });

    const recent = filtered.slice(0, 8);
    const rest = filtered.slice(8);

    return renderFrame({
      title: t('tile.actuator.title'),
      meta: t('tile.actuator.meta'),
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">${t('tile.actuator.hero.entries')}</span>
            <span class="metric cat">${this.entries.length}</span>
          </div>
          <div class="aside">
            <span class="eyebrow">${t('tile.actuator.hero.armed')}</span>
            <span class="stat-pill ${armed > 0 ? 'cat' : ''}">${fmtNum(armed, 0)}</span>
            <span class="eyebrow">${t('tile.actuator.aside.last')}</span>
            <span class="stat-pill">${lastAgeLabel}</span>
          </div>
        </div>

        ${this._renderFilterBar(t)}

        ${filtered.length === 0
          ? html`<div class="empty">${t('tile.actuator.empty')}</div>`
          : html`
            ${this._renderTable(recent, t)}

            ${rest.length > 0 ? html`
              <details>
                <summary>${t('tile.actuator.showall', { count: filtered.length })}</summary>
                <div class="scroll">
                  ${this._renderTable(rest, t)}
                </div>
              </details>
            ` : ''}
          `
        }

        <p class="muted">${t('common.source')}: <code>/api/actuator-journal</code> · ${t('tile.actuator.foot.total', { total: this.total })}</p>
      `,
    });
  }
}

customElements.define('actuator-history-tile', ActuatorHistoryTile);
