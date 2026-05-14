// mode-switcher — 3-state operational mode toggle (cool · quiet · off).
//
// Reads /api/mode every 5s for {mode, cpu_temp_c, throttle_prob}. Writes
// via POST /api/mode. Renders as a nameplate trio matching the masthead
// pill row. While in `quiet`, the daemon's safety belt may eject the
// applied bias the moment Tctl crosses the eject ceiling — we mirror
// that hint with a `⚠ safety` annotation on the active chip when the
// current temperature is approaching the threshold, so the user sees
// *why* the fans just ramped despite the quiet selection.

const MODES = ['cool', 'quiet', 'off'];
const POLL_MS = 5000;
const SAFETY_WARN_TEMP_C = 77;   // soft warn — eject sits at 80°C in decision.py
const SAFETY_EJECT_TEMP_C = 80;

const LABEL = {
  cool:  'COOL',
  quiet: 'QUIET',
  off:   'OFF',
};

const HINT = {
  cool:  'anticipate peaks · fan curve raised before heat',
  quiet: 'subtract bias on calm windows · auto-eject on heat',
  off:   'observe-only · no actuator writes',
};

class ModeSwitcher extends HTMLElement {
  connectedCallback() {
    if (this._mounted) return;
    this._mounted = true;
    this._mode = 'cool';
    this._cpuTemp = null;
    this._throttleProb = null;

    this.classList.add('mode-switcher');
    this.setAttribute('role', 'radiogroup');
    this.setAttribute('aria-label', 'Operational mode');

    const label = document.createElement('span');
    label.className = 'mode-switcher-label';
    label.textContent = 'MODE';
    this.appendChild(label);

    for (const name of MODES) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `mode-chip mode-chip-${name}`;
      btn.dataset.mode = name;
      btn.setAttribute('role', 'radio');
      btn.setAttribute('aria-checked', String(name === this._mode));
      btn.title = HINT[name];
      btn.addEventListener('click', () => this._onPick(name));

      const dot = document.createElement('span');
      dot.className = 'mode-chip-dot';
      btn.appendChild(dot);

      const text = document.createElement('span');
      text.className = 'mode-chip-text';
      text.textContent = LABEL[name];
      btn.appendChild(text);

      this.appendChild(btn);
    }

    // Optional safety annotation chip, hidden when irrelevant. Appended
    // after the buttons so the layout stays stable when it appears.
    this._safetyEl = document.createElement('span');
    this._safetyEl.className = 'mode-safety';
    this._safetyEl.hidden = true;
    this.appendChild(this._safetyEl);

    this._refresh();
    this._timer = setInterval(() => this._refresh(), POLL_MS);
  }

  disconnectedCallback() {
    if (this._timer) clearInterval(this._timer);
  }

  async _onPick(mode) {
    if (mode === this._mode) return;
    // Optimistic: paint immediately, server confirms within the next poll.
    this._mode = mode;
    this._paint();
    try {
      // Per-action route — one verb per URL, atrium-style. Server validates
      // by the path itself; no payload, no client-side schema to keep in
      // sync. Unknown modes are a 404 (route doesn't exist) instead of a
      // 422 (route exists, payload bad) — strictly less ambiguous.
      const r = await fetch(`/api/mode/${encodeURIComponent(mode)}`, {
        method: 'POST',
        cache: 'no-store',
      });
      if (!r.ok) {
        // Roll back on failure — read the canonical value back.
        await this._refresh();
      }
    } catch (_e) {
      await this._refresh();
    }
  }

  async _refresh() {
    try {
      const r = await fetch('/api/mode', { cache: 'no-store' });
      if (!r.ok) return;
      const j = await r.json();
      this._mode = j.mode || 'cool';
      this._cpuTemp = (typeof j.cpu_temp_c === 'number') ? j.cpu_temp_c : null;
      this._throttleProb = (typeof j.throttle_prob === 'number') ? j.throttle_prob : null;
      this._paint();
    } catch (_e) { /* keep last */ }
  }

  _paint() {
    for (const btn of this.querySelectorAll('.mode-chip')) {
      btn.setAttribute('aria-checked', String(btn.dataset.mode === this._mode));
      btn.classList.toggle('is-active', btn.dataset.mode === this._mode);
    }
    // Safety annotation: only meaningful while quiet is active.
    if (this._mode === 'quiet' && this._cpuTemp !== null) {
      if (this._cpuTemp >= SAFETY_EJECT_TEMP_C) {
        this._safetyEl.hidden = false;
        this._safetyEl.textContent = `safety eject · ${this._cpuTemp.toFixed(1)}°C`;
        this._safetyEl.dataset.level = 'eject';
      } else if (this._cpuTemp >= SAFETY_WARN_TEMP_C) {
        this._safetyEl.hidden = false;
        this._safetyEl.textContent = `nearing eject · ${this._cpuTemp.toFixed(1)}°C`;
        this._safetyEl.dataset.level = 'warn';
      } else {
        this._safetyEl.hidden = true;
        this._safetyEl.dataset.level = '';
      }
    } else {
      this._safetyEl.hidden = true;
      this._safetyEl.dataset.level = '';
    }
  }
}

if (!customElements.get('mode-switcher')) {
  customElements.define('mode-switcher', ModeSwitcher);
}
