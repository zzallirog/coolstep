// pill: active workload profile.
//
// Reads /api/profile. The daemon classifies the focused window (hyprctl)
// + top_processes into one of {code, render, game, idle, other}; the curve
// modulates aggressiveness per profile. Pill shows the live label so the
// user can see *which* shape they're currently on without diffing curve
// anchors by hand.

import { $, setPill } from '../lib/dom.js';
import { LangStore, t } from '../i18n/lang-store.js';

const KNOWN = new Set(['code', 'render', 'game', 'idle', 'other']);

const STATE = {
  profile: 'other',
};

async function refresh() {
  try {
    const r = await fetch('/api/profile', { cache: 'no-store' });
    if (!r.ok) {
      STATE.profile = 'other';
      paint();
      return;
    }
    const j = await r.json();
    const raw = j && typeof j === 'object' && j.profile ? String(j.profile) : 'other';
    STATE.profile = KNOWN.has(raw) ? raw : 'other';
  } catch (_e) {
    STATE.profile = 'other';
  }
  paint();
}

function paint() {
  const el = $('pill-profile');
  if (!el) return;
  // Game / Render lean accent (a warmer curve is the «interesting» case);
  // idle stays neutral; code/other neutral.
  const cls =
    STATE.profile === 'game' || STATE.profile === 'render' ? 'accent' : '';
  setPill(el, t(`shell.pill.profile.${STATE.profile}`), cls);
}

export function start({ pollMs = 5000 } = {}) {
  refresh();
  setInterval(refresh, pollMs);
  LangStore.subscribe(paint);
}
