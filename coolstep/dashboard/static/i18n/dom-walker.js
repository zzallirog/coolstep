// i18n DOM walker — applies the static-HTML translation attributes.
//
// Three attribute conventions are honoured:
//   data-i18n="key"        → textContent replaced (safe, no HTML)
//   data-i18n-html="key"   → innerHTML replaced (used for <em>/<strong>)
//   data-i18n-prefix="key" → label-only marker; the pill's value is
//                            painted by JS, this attribute is informational
//                            (aria/static-snapshot only)
//
// The walker is idempotent — calling it again after a language flip simply
// re-paints from the current LangStore.

import { t } from './lang-store.js';

export function applyI18nToDom(root = document) {
  for (const el of root.querySelectorAll('[data-i18n]')) {
    const key = el.getAttribute('data-i18n');
    el.textContent = t(key);
  }
  for (const el of root.querySelectorAll('[data-i18n-html]')) {
    const key = el.getAttribute('data-i18n-html');
    el.innerHTML = t(key);
  }
}
