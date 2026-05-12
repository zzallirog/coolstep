// pill: model drift severity.
//
// Hidden by default — only shown once the daemon has emitted a non-null
// severity. Three bands: < 0.3 ok, < 0.6 muted, >= 0.6 warn.

import { $, setPill } from '../lib/dom.js';
import { LangStore, t } from '../i18n/lang-store.js';

const STATE = { severity: null };

async function refresh() {
  try {
    const r = await fetch('/api/drift', { cache: 'no-store' });
    if (!r.ok) return;
    const j = await r.json();
    if (j.severity != null) STATE.severity = j.severity;
  } catch (_e) { /* keep last */ }
  paint();
}

function paint() {
  const el = $('pill-drift');
  if (!el) return;
  if (STATE.severity == null) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  const cls = STATE.severity >= 0.6 ? 'warn' : STATE.severity < 0.3 ? 'ok' : '';
  setPill(el, `${t('shell.pill.drift')}: ${STATE.severity.toFixed(2)}`, cls);
}

export function start({ pollMs = 60000 } = {}) {
  refresh();
  setInterval(refresh, pollMs);
  LangStore.subscribe(paint);
}
