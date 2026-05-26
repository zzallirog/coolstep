// _orchestrator.js — tile boot orchestrator + shared telemetry poller + fetch budget.
//
// Fixes two root-cause perf issues:
//   1. Duplicate /api/telemetry/latest polling (was 4 independent 1Hz timers).
//   2. ~20 concurrent fetches on mount (browser HTTP/1.1 limits to 6/host,
//      rest stall in the connection pool).
//
// Public API:
//   orchestrator.register(tileName, { priority, mountFn })
//   orchestrator.subscribeTelemetry(callback)         → () unsubscribe
//   orchestrator.fetch(url, opts)                     → Promise<Response>
//   orchestrator.fetchJson(url, fallback, fetchOpts)  → Promise<any>

const PRIORITY = { critical: 0, normal: 1, lazy: 2 };
const STAGGER_MS = 50;             // normal-tile stagger interval
const COALESCE_TTL_MS = 200;       // fetch coalesce window
const CONNECTION_BUDGET = 6;       // matches browser HTTP/1.1 per-host limit
const TELEMETRY_POLL_MS = 1000;    // 1Hz shared poller

// ── Fetch coalescing: same URL within TTL → reuse in-flight Promise ──────────
const _inflight = new Map(); // url → { promise, ts }

function _coalescedFetch(url, opts) {
  const now = Date.now();
  const cached = _inflight.get(url);
  if (cached && now - cached.ts < COALESCE_TTL_MS) {
    return cached.promise;
  }
  const p = fetch(url, opts).finally(() => {
    if (_inflight.get(url)?.promise === p) _inflight.delete(url);
  });
  _inflight.set(url, { promise: p, ts: now });
  return p;
}

// ── Connection budget: token-bucket limiting concurrent fetches ───────────────
let _inFlightCount = 0;
const _fetchQueue = []; // { url, opts, resolve, reject, priority }

function _drainQueue() {
  while (_fetchQueue.length > 0 && _inFlightCount < CONNECTION_BUDGET) {
    // Sort by priority: lower number = higher priority (critical=0 first)
    _fetchQueue.sort((a, b) => (a.priority || 1) - (b.priority || 1));
    const item = _fetchQueue.shift();
    _inFlightCount += 1;
    _coalescedFetch(item.url, item.opts)
      .then((r) => item.resolve(r))
      .catch((err) => item.reject(err))
      .finally(() => {
        _inFlightCount -= 1;
        _drainQueue();
      });
  }
}

function _budgetedFetch(url, opts, priority) {
  if (_inFlightCount < CONNECTION_BUDGET) {
    _inFlightCount += 1;
    const p = _coalescedFetch(url, opts);
    p.finally(() => {
      _inFlightCount -= 1;
      _drainQueue();
    });
    return p;
  }
  return new Promise((resolve, reject) => {
    _fetchQueue.push({ url, opts, resolve, reject, priority: priority ?? 1 });
  });
}

// ── Shared telemetry poller (replaces 4× independent 1Hz timers) ─────────────
const _telemetrySubscribers = new Set();
let _telemetryLastTs = null;
let _telemetryTimerId = null;

async function _pollTelemetryOnce() {
  if (_telemetrySubscribers.size === 0) return;
  try {
    const r = await _budgetedFetch('/api/telemetry/latest', { cache: 'no-store' }, PRIORITY.critical);
    if (!r.ok) return;
    const data = await r.json();
    if (!data) return;
    // Dedup REMOVED 2026-05-25: server cache TTL=1s + poller 1Hz + collector
    // 1.5Hz aliased to same body → tiles froze 6-7s. Idempotent re-render is cheap.
    _telemetryLastTs = data.ts;
    for (const cb of _telemetrySubscribers) {
      try { cb(data); } catch (_e) { /* tile error, don't break others */ }
    }
  } catch (_e) { /* network transient */ }
}

function _startTelemetryPoller() {
  if (_telemetryTimerId !== null) return;
  _telemetryTimerId = setInterval(() => {
    // Skip polls when tab hidden — saves ~60 fetches/min of pure waste when
    // user is on another tab. visibilitychange listener (registered once
    // below) fires immediate poll on return so resume isn't laggy.
    if (typeof document !== 'undefined' && document.hidden) return;
    _pollTelemetryOnce();
  }, TELEMETRY_POLL_MS);
}

if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) _pollTelemetryOnce();
  });
}

// ── Tile registry + per-priority mount on register ───────────────────────────
//
// History: an earlier design snapshotted the registry on a "boot" microtask
// after the first register() call. That raced with module script execution:
// modules run as separate tasks, the snapshot drained before later modules
// registered, and 18/19 tiles sat unmounted forever (visible empty cards
// despite live daemon). Replaced with per-tile mount-on-register — boot has
// no global synchronization point any more, the connection budget already
// handles burst protection.

const _registry = [];      // { name, priority, mountFn, element, mounted }
let _staggerIdx = 0;       // monotonic counter for normal-tile stagger spread
let _lazyObserver = null;  // single IntersectionObserver shared across lazy tiles

function _mountByPriority(entry) {
  if (entry.mounted) return;

  if (entry.priority === PRIORITY.critical) {
    _mount(entry);
    return;
  }

  if (entry.priority === PRIORITY.normal) {
    // Spread mounts across the idle window so we don't burst-fetch 18 endpoints
    // in one frame. Each new normal tile claims the next stagger slot.
    const idx = _staggerIdx++;
    const _idle = typeof requestIdleCallback === 'function' ? requestIdleCallback : (cb) => setTimeout(cb, 0);
    _idle(() => setTimeout(() => _mount(entry), idx * STAGGER_MS));
    return;
  }

  // PRIORITY.lazy — wait until tile scrolls near viewport, or 2s fallback.
  if (entry.element && typeof IntersectionObserver !== 'undefined') {
    if (_lazyObserver === null) {
      _lazyObserver = new IntersectionObserver((entries) => {
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          const t = _registry.find((reg) => reg.element === e.target);
          if (t && !t.mounted) {
            _mount(t);
            _lazyObserver.unobserve(e.target);
          }
        }
      }, { rootMargin: '200px 0px', threshold: 0 });
    }
    _lazyObserver.observe(entry.element);
  } else {
    setTimeout(() => _mount(entry), 2000);
  }
}

function _mount(tile) {
  if (tile.mounted) return;
  tile.mounted = true;
  try { tile.mountFn(); } catch (_e) { /* tile error isolated */ }
}

// ── Per-route failure tracking — emit global event after N consecutive fails ─
const _routeFails = new Map(); // url → consecutive-fail count
const DEGRADE_THRESHOLD = 3;

function _markFetch(url, ok) {
  if (typeof document === 'undefined') return;
  if (ok) {
    if (_routeFails.get(url)) {
      _routeFails.delete(url);
      document.dispatchEvent(new CustomEvent('coolstep:api-recovered', { detail: { url } }));
    }
    return;
  }
  const n = (_routeFails.get(url) || 0) + 1;
  _routeFails.set(url, n);
  if (n === DEGRADE_THRESHOLD) {
    document.dispatchEvent(new CustomEvent('coolstep:api-degraded', { detail: { url, fails: n } }));
  }
}

// ── Public singleton ─────────────────────────────────────────────────────────
export const orchestrator = {
  /** Register a tile. Call from connectedCallback before starting own timers. */
  register(name, { priority = 'normal', mountFn, element = null } = {}) {
    const p = PRIORITY[priority] ?? PRIORITY.normal;
    // Race 5 fix: tile may re-connect (SPA nav, devtools, browser back/fwd).
    // Without dedup the second register() pushes a duplicate; with per-tile
    // mount-on-register we'd also re-stagger an already-mounted tile.
    const existing = _registry.find((e) => e.name === name);
    if (existing) {
      existing.priority = p;
      existing.mountFn = mountFn;
      existing.element = element;
      if (existing.mounted) {
        // Already-mounted tile reconnected — re-fire mount now.
        existing.mounted = false;
        _mount(existing);
      }
      return existing;
    }
    const entry = { name, priority: p, mountFn, element, mounted: false };
    _registry.push(entry);
    _mountByPriority(entry);
    _startTelemetryPoller();
    return entry;
  },

  /** Subscribe to 1Hz telemetry frames. Returns an unsubscribe function. */
  subscribeTelemetry(callback) {
    _telemetrySubscribers.add(callback);
    _startTelemetryPoller();
    return () => _telemetrySubscribers.delete(callback);
  },

  /** Budgeted + coalesced fetch. Prefer this over bare fetch() in tiles. */
  fetch(url, opts, priority = 'normal') {
    const p = PRIORITY[priority] ?? PRIORITY.normal;
    return _budgetedFetch(url, opts ?? { cache: 'no-store' }, p);
  },

  /** Like fetchJson in _base.js but goes through the connection budget. */
  async fetchJson(url, fallback = null, { retry = 1, retryDelayMs = 250, priority = 'normal' } = {}) {
    const p = PRIORITY[priority] ?? PRIORITY.normal;
    for (let attempt = 0; attempt <= retry; attempt += 1) {
      try {
        const r = await _budgetedFetch(url, { cache: 'no-store' }, p);
        if (r.ok) {
          _markFetch(url, true);
          return await r.json();
        }
      } catch (_e) { /* fallthrough */ }
      if (attempt < retry) await new Promise((res) => setTimeout(res, retryDelayMs));
    }
    _markFetch(url, false);
    return fallback;
  },
};
