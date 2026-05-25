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

function _startTelemetryPoller() {
  if (_telemetryTimerId !== null) return;
  _telemetryTimerId = setInterval(async () => {
    if (_telemetrySubscribers.size === 0) return;
    try {
      const r = await _budgetedFetch('/api/telemetry/latest', { cache: 'no-store' }, PRIORITY.critical);
      if (!r.ok) return;
      const data = await r.json();
      if (!data) return;
      // Dedup REMOVED 2026-05-25: server has 1s cache TTL + poller fires 1Hz +
      // collector at 1.5Hz creates phase aliasing → multiple consecutive polls
      // returned same cached body with same ts → emit suppressed → tiles froze
      // for 6-7s while client-side age timer ticked independently 30s→36s.
      // Idempotent re-render in subscribers is cheap; let every poll emit.
      _telemetryLastTs = data.ts;
      for (const cb of _telemetrySubscribers) {
        try { cb(data); } catch (_e) { /* tile error, don't break others */ }
      }
    } catch (_e) { /* network transient */ }
  }, TELEMETRY_POLL_MS);
}

// ── Tile registry + staggered mount ──────────────────────────────────────────
const _registry = [];      // { name, priority, mountFn, mounted }
let _bootScheduled = false;

function _scheduleBoot() {
  if (_bootScheduled) return;
  _bootScheduled = true;
  // Defer to next microtask so all connectedCallback registrations arrive first.
  Promise.resolve().then(_runBoot);
}

function _runBoot() {
  const critical = _registry.filter((t) => t.priority === PRIORITY.critical && !t.mounted);
  const normal   = _registry.filter((t) => t.priority === PRIORITY.normal   && !t.mounted);
  const lazy     = _registry.filter((t) => t.priority === PRIORITY.lazy     && !t.mounted);

  // Critical: mount immediately
  for (const tile of critical) {
    _mount(tile);
  }

  // Normal: stagger at STAGGER_MS intervals using rIC with setTimeout fallback
  const _idle = typeof requestIdleCallback === 'function' ? requestIdleCallback : (cb) => setTimeout(cb, 0);
  normal.forEach((tile, idx) => {
    _idle(() => setTimeout(() => _mount(tile), idx * STAGGER_MS));
  });

  // Lazy: defer until IntersectionObserver fires (or 2s fallback for no-IO envs)
  const io = typeof IntersectionObserver !== 'undefined'
    ? new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const tile = lazy.find((t) => t.element === entry.target);
          if (tile && !tile.mounted) {
            _mount(tile);
            io.unobserve(entry.target);
          }
        }
      }, { rootMargin: '200px 0px', threshold: 0 })
    : null;

  for (const tile of lazy) {
    if (io && tile.element) {
      io.observe(tile.element);
    } else {
      // Fallback: mount after 2s
      setTimeout(() => _mount(tile), 2000);
    }
  }
}

function _mount(tile) {
  if (tile.mounted) return;
  tile.mounted = true;
  try { tile.mountFn(); } catch (_e) { /* tile error isolated */ }
}

// ── Public singleton ─────────────────────────────────────────────────────────
export const orchestrator = {
  /** Register a tile. Call from connectedCallback before starting own timers. */
  register(name, { priority = 'normal', mountFn, element = null } = {}) {
    const p = PRIORITY[priority] ?? PRIORITY.normal;
    const entry = { name, priority: p, mountFn, element, mounted: false };
    _registry.push(entry);
    _scheduleBoot();
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
        if (r.ok) return await r.json();
      } catch (_e) { /* fallthrough */ }
      if (attempt < retry) await new Promise((res) => setTimeout(res, retryDelayMs));
    }
    return fallback;
  },
};
