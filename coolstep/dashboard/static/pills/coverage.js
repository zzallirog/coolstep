// pill: calibration coverage + mode-readiness.
//
// Reads /api/calibration. Two pieces of UI ride on this endpoint:
//   pill-mode      → calibration `ready` flag (calibrating | ready)
//   pill-coverage  → coverage_hours gate progress + percentage

import { $, setPill } from '../lib/dom.js';
import { LangStore, t } from '../i18n/lang-store.js';

const STATE = {
  ready: false,
  pct: null,
  current: null,
  target: null,
  passed: false,
};

async function refresh() {
  try {
    const r = await fetch('/api/calibration', { cache: 'no-store' });
    if (!r.ok) return;
    const j = await r.json();
    STATE.ready = Boolean(j.ready);
    const cov = j.gates?.coverage_hours;
    if (cov) {
      STATE.pct = Math.min(100, Math.round((cov.current / cov.target) * 100));
      STATE.current = cov.current;
      STATE.target = cov.target;
      STATE.passed = Boolean(cov.passed);
    }
  } catch (_e) { /* keep last */ }
  paint();
}

function paint() {
  const modeEl = $('pill-mode');
  if (modeEl) {
    setPill(modeEl,
      STATE.ready ? t('shell.pill.mode.ready') : t('shell.pill.mode.calibrating'),
      STATE.ready ? 'ok' : '');
  }
  const covEl = $('pill-coverage');
  if (covEl && STATE.pct != null) {
    setPill(covEl,
      `${t('shell.pill.coverage')}: ${STATE.pct}% (${STATE.current.toFixed(1)}h / ${STATE.target}h)`,
      STATE.passed ? 'ok' : '');
  }
}

export function start({ pollMs = 30000 } = {}) {
  refresh();
  setInterval(refresh, pollMs);
  LangStore.subscribe(paint);
}
