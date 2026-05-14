// pill: discovered signals count.
//
// Reads /api/discoveries (Zabbix-LLD-style manifest aggregated from every
// collector's signals() method). The dashboard cares about one number —
// the total — but the endpoint itself is the source-of-truth catalogue.

import { $, setPill } from '../lib/dom.js';
import { LangStore, t } from '../i18n/lang-store.js';

const STATE = { total: null };

async function refresh() {
  try {
    const r = await fetch('/api/discoveries', { cache: 'no-store' });
    if (!r.ok) return;
    const j = await r.json();
    STATE.total = j.total ?? 0;
  } catch (_e) { /* keep last */ }
  paint();
}

function paint() {
  const el = $('pill-signals');
  if (el && STATE.total != null) {
    setPill(el, `${t('shell.pill.signals')}: ${STATE.total}`, 'accent');
  }
}

export function start({ pollMs = 60000 } = {}) {
  refresh();
  setInterval(refresh, pollMs);
  LangStore.subscribe(paint);
}
