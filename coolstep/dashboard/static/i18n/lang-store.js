/* LangStore — singleton holding the active language code + a subscriber set.
 *
 * Lit components hook in via LangController (see below). Vanilla DOM (legacy
 * dashboard.js, masthead pills) hooks in via direct subscribe() + DOM walk.
 *
 * Persistence:
 *   1. localStorage 'coolstep-lang' (manual user choice — wins)
 *   2. navigator.language hint on first load (best-effort: en|ru|uk)
 *   3. fallback 'en'
 *
 * Strings missing in the active language fall back to en, then to the key.
 */
import { STRINGS, LANGS } from './strings.js';

const STORAGE_KEY = 'coolstep-lang';
const SUPPORTED = new Set(LANGS.map((l) => l.code));

function detectInitial() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && SUPPORTED.has(saved)) return saved;
  } catch (_e) { /* private mode etc */ }
  const nav = (navigator.language || 'en').slice(0, 2).toLowerCase();
  if (SUPPORTED.has(nav)) return nav;
  return 'en';
}

class LangStoreImpl {
  constructor() {
    this.lang = detectInitial();
    this._subs = new Set();
    // Expose for non-module DOM (legacy dashboard.js)
    if (typeof window !== 'undefined') {
      window.__coolstepLang = this;
    }
    document.documentElement.lang = this.lang;
  }

  set(code) {
    if (!SUPPORTED.has(code) || code === this.lang) return;
    this.lang = code;
    document.documentElement.lang = code;
    try { localStorage.setItem(STORAGE_KEY, code); } catch (_e) {}
    for (const fn of this._subs) {
      try { fn(code); } catch (_e) {}
    }
  }

  subscribe(fn) {
    this._subs.add(fn);
    return () => this._subs.delete(fn);
  }

  /** Lookup with active-lang → en → key fallback. Interpolates {placeholders}. */
  t(key, vars) {
    let raw = STRINGS[this.lang]?.[key];
    if (raw == null) raw = STRINGS.en[key];
    if (raw == null) return key;
    if (!vars) return raw;
    return raw.replace(/\{(\w+)\}/g, (m, k) => (vars[k] != null ? String(vars[k]) : m));
  }
}

export const LangStore = new LangStoreImpl();
export const t = LangStore.t.bind(LangStore);
export { LANGS };

/* LangController — Lit ReactiveController. A tile subscribes via
 *   this._lang = new LangController(this);
 * and any `t('...')` call inside render() will re-run when language flips.
 *
 * Pattern lifted from atrium dashboard; kept tiny — no extra deps. */
export class LangController {
  constructor(host) {
    this.host = host;
    host.addController(this);
  }
  hostConnected() {
    this._unsub = LangStore.subscribe(() => this.host.requestUpdate());
  }
  hostDisconnected() {
    if (this._unsub) this._unsub();
  }
  get code() { return LangStore.lang; }
  t(key, vars) { return LangStore.t(key, vars); }
}
