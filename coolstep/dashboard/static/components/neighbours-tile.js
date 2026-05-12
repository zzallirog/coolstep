import { LitElement, html, css, fetchJson, fmtNum, tileBaseStyles, renderFrame } from './_base.js';

/** Top-K nearest historical frames from ChromaDB. Replaces the «Predictions live»
 * placeholder — once embedder fits, neighbours start appearing. */
export class NeighboursTile extends LitElement {
  static styles = [
    tileBaseStyles,
    css`
      .reason {
        padding: 10px 14px;
        background: color-mix(in srgb, var(--cat-color) 8%, transparent);
        border-left: 3px solid var(--cat-color);
        border-radius: var(--radius-sm);
        font-size: 13px;
        color: var(--fg);
        font-style: italic;
      }
      table { width: 100%; font-size: 12px; border-collapse: collapse;
              font-family: var(--font-mono, monospace); }
      th { color: var(--fg-muted); font-weight: 600; text-align: left;
           padding: 6px 8px 6px 0; border-bottom: 1px solid var(--border);
           font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; }
      td { padding: 5px 8px 5px 0; border-bottom: 1px dotted var(--border-soft);
           font-variant-numeric: tabular-nums; }
      td.dist { color: var(--cat-color); }
      td.label-hot { color: var(--warn); font-weight: 600; }
      td.label-cool { color: var(--ok); font-weight: 600; }
      td.label-unknown { color: var(--fg-dim); }
    `,
  ];

  static properties = {
    state: { state: true },
  };

  constructor() {
    super();
    this.state = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._refresh();
    this._timer = setInterval(() => this._refresh(), 5000);
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
  }

  async _refresh() {
    this.state = await fetchJson('/api/neighbours', { neighbours: [] });
  }

  _formatTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    const ago = Math.round(Date.now() / 1000 - ts);
    const hms = d.toISOString().slice(11, 19);
    if (ago < 3600) return `${Math.round(ago / 60)}m ago`;
    if (ago < 86400) return `${(ago / 3600).toFixed(1)}h ago`;
    return `${(ago / 86400).toFixed(1)}d ago · ${hms}`;
  }

  _label(was_hot) {
    if (was_hot === 1) return { cls: 'label-hot', text: 'hot' };
    if (was_hot === 0) return { cls: 'label-cool', text: 'cool' };
    return { cls: 'label-unknown', text: '?' };
  }

  render() {
    const s = this.state || {};
    const ns = s.neighbours || [];
    const prob = (s.throttle_prob || 0) * 100;
    const conf = (s.confidence || 0) * 100;

    if (!s.embedder_fitted) {
      return renderFrame({
        title: 'Predictions — neighbours',
        body: html`
          <div class="empty">
            Embedder cold-start: collecting first 60 frames before normalization stats stabilize.
            ChromaDB count: ${s.chroma_count ?? 0}.
          </div>
        `,
      });
    }
    // KNN dormant: embedder is fitted but the vector store is empty.
    // On this host that's the masked-by-flag ChromaDB SEGV state — see
    // CLAUDE.md `COOLSTEP_CHROMA_DISABLED=1`.  Distinct from the genuine
    // "warming" case (chroma alive, just hasn't accumulated enough
    // labelled neighbours yet) — we tell those apart by chroma_count.
    // The reason string we'd otherwise echo here belongs to whichever
    // predictor IS running (trajectory_baseline+meta in dormant state),
    // which is already shown in <predictor-cockpit-tile>; repeating it
    // here just confused the KNN-versus-trajectory mental model.
    if (ns.length === 0) {
      const dormant = (s.chroma_count ?? 0) === 0;
      const activeModel = s.model || 'unknown';
      return renderFrame({
        title: 'Predictions — neighbours',
        meta: dormant ? 'KNN dormant' : 'warming',
        body: html`
          <div class="empty">
            ${dormant
              ? html`
                  KNN dormant · vector store empty (ChromaDB disabled).<br>
                  Active predictor: <strong>${activeModel}</strong>
                  — live state in &lt;predictor-cockpit-tile&gt;.<br>
                  Restore: see TODO.md «ChromaDB SEGV».
                `
              : html`
                  Embedder fitted (${s.chroma_count ?? 0} vecs stored),
                  but no labelled neighbours surfaced yet — store is
                  warming.
                `}
          </div>
        `,
      });
    }
    const probCls = prob >= 70 ? 'err' : prob >= 40 ? 'warn' : 'ok';
    return renderFrame({
      title: 'Predictions — neighbours',
      meta: s.model || 'knn_v1',
      body: html`
        <div class="hero">
          <div class="col">
            <span class="eyebrow cat">throttle prob · 30s</span>
            <span class="metric ${probCls}">${fmtNum(prob, 0)}<small>%</small></span>
          </div>
          <div class="col">
            <span class="eyebrow">confidence</span>
            <span class="metric">${fmtNum(conf, 0)}<small>%</small></span>
          </div>
          <div class="aside">
            <span class="eyebrow">store</span>
            <span class="stat-pill cat">${fmtNum(s.chroma_count, 0)} vecs</span>
          </div>
        </div>

        ${s.reason ? html`<p class="reason">${s.reason}</p>` : ''}

        <table>
          <thead>
            <tr>
              <th>when</th>
              <th>dist</th>
              <th>cpu now</th>
              <th>after 30s</th>
              <th>label</th>
              <th>workload</th>
            </tr>
          </thead>
          <tbody>
            ${ns.map((n) => {
              const lbl = this._label(n.was_hot_in_30s);
              return html`
                <tr>
                  <td>${this._formatTime(n.ts)}</td>
                  <td class="dist">${fmtNum(n.distance, 3)}</td>
                  <td>${fmtNum(n.cpu_temp_at, 1, '°C')}</td>
                  <td>${n.peak_temp_after && n.peak_temp_after > 0
                        ? fmtNum(n.peak_temp_after, 1, '°C') : '—'}</td>
                  <td class=${lbl.cls}>${lbl.text}</td>
                  <td>${n.workload_label || '—'}</td>
                </tr>
              `;
            })}
          </tbody>
        </table>
        <p class="muted">Top-${ns.length} cosine neighbours · refresh 5s · proxy label = (peak_temp ≥ 90°C in next 30s)</p>
      `,
    });
  }
}

customElements.define('neighbours-tile', NeighboursTile);
