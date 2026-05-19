// Theme switcher: six paint-chip swatches that flip `data-theme` on
// <html>. Choice persists in localStorage; the FOUC-guard inline script
// in index.html applies the saved theme synchronously before CSS loads,
// so the dashboard never paints in the wrong palette on reload.

const THEMES = ['graphite', 'amber', 'blueprint', 'parchment', 'obsidian', 'redline'];
const STORAGE_KEY = 'coolstep.theme';
const DEFAULT_THEME = 'graphite';

export function getTheme() {
  try {
    const t = localStorage.getItem(STORAGE_KEY);
    return THEMES.includes(t) ? t : DEFAULT_THEME;
  } catch (_e) {
    return DEFAULT_THEME;
  }
}

export function setTheme(name) {
  const t = THEMES.includes(name) ? name : DEFAULT_THEME;
  if (t === DEFAULT_THEME) {
    document.documentElement.removeAttribute('data-theme');
  } else {
    document.documentElement.setAttribute('data-theme', t);
  }
  try { localStorage.setItem(STORAGE_KEY, t); } catch (_e) {}
  document.dispatchEvent(new CustomEvent('coolstep:theme-change', { detail: { theme: t } }));
}

class ThemeSwitcher extends HTMLElement {
  connectedCallback() {
    if (this._mounted) return;
    this._mounted = true;
    this.classList.add('theme-switcher');
    this.setAttribute('role', 'radiogroup');
    this.setAttribute('aria-label', 'Theme');

    const label = document.createElement('span');
    label.className = 'theme-switcher-label';
    label.textContent = 'TONE';
    this.appendChild(label);

    const current = getTheme();
    for (const name of THEMES) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'theme-swatch';
      btn.dataset.theme = name;
      btn.setAttribute('role', 'radio');
      btn.setAttribute('aria-label', name);
      btn.title = name;
      btn.setAttribute('aria-pressed', String(name === current));
      btn.addEventListener('click', () => this._onPick(name));
      this.appendChild(btn);
    }

    document.addEventListener('coolstep:theme-change', (e) => this._syncPressed(e.detail.theme));
  }

  _onPick(name) {
    setTheme(name);
  }

  _syncPressed(active) {
    for (const el of this.querySelectorAll('.theme-swatch')) {
      el.setAttribute('aria-pressed', String(el.dataset.theme === active));
    }
  }
}

if (!customElements.get('theme-switcher')) {
  customElements.define('theme-switcher', ThemeSwitcher);
}
