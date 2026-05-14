// pill: stress-test runner state.
//
// Reads /api/stress-state. When the bench harness is firing a scenario the
// pill carries the scenario name + remaining seconds, coloured by whether
// the run is in armed mode (actuator writes ON) or dry-run.

import { $, setPill } from '../lib/dom.js';
import { LangStore, t } from '../i18n/lang-store.js';

const STATE = {
  active: false,
  scenario: null,
  armed: false,
  endsAt: null,
};

async function refresh() {
  try {
    const r = await fetch('/api/stress-state', { cache: 'no-store' });
    if (!r.ok) {
      STATE.active = false; STATE.scenario = null; STATE.armed = false; STATE.endsAt = null;
      paint();
      return;
    }
    const j = await r.json();
    if (!j || typeof j !== 'object' || !j.scenario || j.started_at == null) {
      STATE.active = false; STATE.scenario = null; STATE.armed = false; STATE.endsAt = null;
    } else {
      STATE.active = true;
      STATE.scenario = String(j.scenario);
      STATE.armed = Boolean(j.armed);
      STATE.endsAt = Number(j.started_at) + Number(j.duration_sec || 0);
    }
  } catch (_e) {
    STATE.active = false;
  }
  paint();
}

function paint() {
  const el = $('pill-stress');
  if (!el) return;
  if (!STATE.active) return setPill(el, t('shell.pill.stress.idle'), '');
  const remaining = STATE.endsAt != null
    ? Math.max(0, Math.round(STATE.endsAt - Date.now() / 1000))
    : 0;
  setPill(el,
    t('shell.pill.stress.running', { scenario: STATE.scenario || '?', remaining }),
    STATE.armed ? 'accent' : 'warn');
}

export function start({ pollMs = 5000 } = {}) {
  refresh();
  setInterval(refresh, pollMs);
  LangStore.subscribe(paint);
}
