// pill: crash-recovery indicator.
//
// Reads /api/crash-recovery. Hidden when the daemon has no recovery record.
// Within 24h of a crash the pill is `warn`-coloured; older fades to muted
// so the badge of dishonour doesn't haunt the masthead forever.

import { $, setPill } from '../lib/dom.js';
import { LangStore, t } from '../i18n/lang-store.js';

const STATE = { age: null, kind: null, recovered: false };

function humanAge(sec) {
  if (sec < 60)    return `${Math.round(sec)}s`;
  if (sec < 3600)  return `${Math.round(sec / 60)}m`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h`;
  return `${Math.round(sec / 86400)}d`;
}

async function refresh() {
  try {
    const r = await fetch('/api/crash-recovery', { cache: 'no-store' });
    if (!r.ok) {
      STATE.recovered = false;
      paint();
      return;
    }
    const j = await r.json();
    STATE.recovered = Boolean(j.recovered);
    STATE.age = j.age_sec ?? 0;
    STATE.kind = j.kind ?? 'unknown';
  } catch (_e) {
    STATE.recovered = false;
  }
  paint();
}

function paint() {
  const el = $('pill-crash');
  if (!el) return;
  if (!STATE.recovered) {
    el.hidden = true;
    el.className = 'pill';
    return;
  }
  el.hidden = false;
  if (STATE.age < 86400) {
    setPill(el, t('shell.pill.crash.recent', { age: humanAge(STATE.age), kind: STATE.kind }), 'warn');
  } else {
    setPill(el, t('shell.pill.crash.stale', { age: humanAge(STATE.age), kind: STATE.kind }), '');
  }
}

export function start({ pollMs = 60000 } = {}) {
  refresh();
  setInterval(refresh, pollMs);
  LangStore.subscribe(paint);
}
