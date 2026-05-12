// pill: daemon health.
//
// Lifecycle:
//   refresh → fetch /api/health → derive code → paint.
// A 2-miss streak is required before flipping to 'failed' — single transient
// fetch errors (e.g. during a daemon restart) shouldn't shout "daemon down"
// to the user.

import { $, setPill } from '../lib/dom.js';
import { LangStore, t } from '../i18n/lang-store.js';

const STATE = { code: 'checking', age: null };
let missStreak = 0;

async function refresh() {
  try {
    const r = await fetch('/api/health', { cache: 'no-store' });
    const j = await r.json();
    missStreak = 0;
    if (!j.daemon_seen) {
      STATE.code = 'not_seen'; STATE.age = null;
    } else if (j.ml_state_age_sec != null && j.ml_state_age_sec > 60) {
      STATE.code = 'stale'; STATE.age = j.ml_state_age_sec;
    } else {
      STATE.code = 'ok'; STATE.age = null;
    }
  } catch (_e) {
    missStreak += 1;
    if (missStreak >= 2) { STATE.code = 'failed'; STATE.age = null; }
  }
  paint();
}

function paint() {
  const el = $('pill-health');
  if (!el) return;
  switch (STATE.code) {
    case 'checking': return setPill(el, t('shell.pill.daemon.checking'), '');
    case 'ok':       return setPill(el, t('shell.pill.daemon.ok'), 'ok');
    case 'stale':    return setPill(el, `${t('shell.pill.daemon.stale')} ${Math.round(STATE.age)}s`, 'warn');
    case 'not_seen': return setPill(el, t('shell.pill.daemon.not_seen'), 'err');
    case 'failed':   return setPill(el, t('shell.pill.daemon.failed'), 'err');
  }
}

export function start({ pollMs = 10000 } = {}) {
  refresh();
  setInterval(refresh, pollMs);
  LangStore.subscribe(paint);
}
