// Tiny DOM helpers shared by every pill module. Kept deliberately small —
// these are utilities, not a framework. Anything fancier earns its own
// file (one job per module is the architectural budget).

export function $(id) {
  return document.getElementById(id);
}

/** Paint a masthead pill with text + class. `cls` ∈ ''|'ok'|'warn'|'err'|'accent'.
 *  The chip layout is owned by styles.css `.pill[.ok|.warn|.err|.accent]` — we
 *  only flip the modifier. No-op if the element is missing (a pill can be
 *  conditionally hidden in markup; we don't want to fail loud). */
export function setPill(el, text, cls = '') {
  if (!el) return;
  el.textContent = text;
  el.className = 'pill ' + cls;
}
