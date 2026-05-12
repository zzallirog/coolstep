// pill: registered actuators count.
//
// Why /api/ml-state and not /api/adapters? /api/adapters spawns a FRESH
// discover_actuators() inside the dashboard process every call — and the
// dashboard's environment doesn't carry the COOLSTEP_ACTUATOR_ENABLE drop-in
// that the collector unit sets, so make() returns None for the live
// actuators and the dashboard sees zero. The DAEMON, which runs with the
// drop-in, writes its actual registered actuators into ml-state.json.
// Reading from there gives us "what the daemon *really* has armed", which
// is what the user wants to see in the pill.

import { $, setPill } from '../lib/dom.js';
import { LangStore, t } from '../i18n/lang-store.js';

const STATE = { total: null, names: [] };

async function refresh() {
  try {
    const r = await fetch('/api/ml-state', { cache: 'no-store' });
    if (!r.ok) return;
    const j = await r.json();
    const names = Array.isArray(j.actuators) ? j.actuators.slice() : [];
    STATE.names = names;
    STATE.total = names.length;
  } catch (_e) { /* keep last */ }
  paint();
}

function paint() {
  const el = $('pill-actuators');
  if (!el || STATE.total == null) return;
  setPill(el, `${t('shell.pill.actuators')}: ${STATE.total}`, STATE.total > 0 ? 'accent' : '');
  el.title = STATE.names.join(', ') || '—';
}

export function start({ pollMs = 30000 } = {}) {
  refresh();
  setInterval(refresh, pollMs);
  LangStore.subscribe(paint);
}
