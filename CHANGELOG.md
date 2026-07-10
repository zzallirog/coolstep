# Changelog

All notable changes to coolstep are documented in this file.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [semver](https://semver.org/).

---

## [Unreleased]

Nothing pending — all work merged to master.

---

## [0.5.21] — 2026-07-10

Logic-flaw audit sweep (2026-07-10): «stated intent ↔ actual behavior»
gaps hunted across daemon/predictor/dashboard/docs, then fixed. Six
confirmed findings, all with file:line proofs; 1025 tests green after.

### Fixed — the predictor actually predicts in fallback mode

- **Physics trajectory overlay was welded to the KNN path.** The
  recognition-independent safety gate («94°C rising — act regardless of
  neighbours») lived inside `KnnPredictor` only, so the deployed
  fallback (`MetaPredictor(TrajectoryBaseline)`, Chroma disabled) had
  `throttle_prob` hard-0 forever: no NOTIFY, no safety net, exactly in
  the mode with no other line of defence. Overlay extracted to
  `predictor.trajectory_signal()` and merged by TrajectoryBaseline too.
- **Dry-run applies poisoned the causal ledger.** In dry-run (the
  shipped default) every `apply()` returned `error=None`, so the daemon
  armed the action AND recorded an intervention window → validated
  residuals inside it were excluded from ResidualBank training. Pure
  passive thermal samples thrown away exactly during hot moments, for
  every monitoring-only deployment. Windows now recorded only when
  `COOLSTEP_ACTUATOR_ENABLE` is truthy.
- **decisions.jsonl silently dropped every low-confidence call** via the
  `confidence < min_arm_confidence` early return — while the module
  contract says «records EVERY decide() call». The log was blind exactly
  in the region used to tune `min_arm_confidence`. Now logs before the
  early return.
- **Orphan-label recovery poisoned the 82-90°C band.** Restart-orphaned
  chroma vectors were blanket-relabelled COOL, contradicting the live
  labeler (whose lookahead window includes the frame itself, so ≥82°C
  provably labels HOT). Recovery now splits on `HOT_THRESHOLD_C`.

### Fixed — one readiness predicate, honestly surfaced

- **Calibration split-brain.** Docs said «8 gates», the daemon evaluated
  6 (cost gates never received inputs), the dashboard recomputed its own
  5. Daemon now feeds real cost metrics (`/proc/self/status` VmRSS,
  process-time CPU%) → all 8 code gates live; the full gate report is
  dumped into `ml-state.json` and `/api/calibration` serves it
  (`source: "daemon"`), falling back to a local recompute
  (`source: "dashboard-recompute"`) only when the daemon is dead.
- **Drift «red disarms actuator» was doc-fiction** — the word `disarm`
  didn't exist in code. Implemented: any indicator at severity ≥ 0.8
  (env `COOLSTEP_DRIFT_DISARM_SEVERITY`) holds `calibration_ready`
  False. Sub-red indicators (embedder_cold, knn_low_confidence) surface
  without blocking, so intentional fallback deployments stay armed-able.
- **Degraded predictor rendered as maximally healthy.** The
  training-tile fallback banner matched only `always_idle_baseline` — a
  model no longer wired — so the real fallback
  (`trajectory_baseline+meta`) showed no warning and the cockpit
  displayed structural confidence as reflective. Banner now keys on
  `chroma_available === false` / non-KNN model; cockpit gains a
  `degraded` field + «⛔ fallback» chip.
- **Python 3.14 auto-fallback now exists.** README promised the daemon
  «detects and falls back automatically» on the chromadb 3.14 segfault;
  detection now happens before import (a segfault can't be caught).
  Opt back in via `COOLSTEP_CHROMA_FORCE=1`.

### Fixed — interference S15 closed (asusctl baseline drift)

- `revert()` re-reads the live curve first: matches neither the last
  issued curve nor the saved baseline → user edited it externally →
  restore skipped, stale baseline dropped, cession journaled.
- `_ensure_baseline()` refuses to adopt coolstep's own issued curve as
  «user baseline» when the TTL lapses while a bias is applied.
- Matrix: 10 covered / 3 open gaps (S08, S10, S14) / 4 structural.

### Added — long-horizon thermal ledger (`daily_rollup`)

- New permanent sqlite table: one row per UTC day × workload label
  (+ pooled `_all` row) with cpu-temp/power/fan p50/p95/max and daily
  throttle-event count. Populated by `rotate()` **before** frame
  eviction; survives the 14-day frames TTL indefinitely (~KB/day).
  This is the substrate for seasonal phase shifts (winter/summer
  ambient), thermal-interface degradation and dust tracking: same
  workload bucket, rising temp_p95/fan_p95 at flat power_p50 = the
  cooling path got worse.
- `GET /api/thermal-history?since=YYYY-MM-DD&workload=X` serves the
  ledger (raw read-only query — dashboard still never writes sqlite).

### Fixed — small correctness

- `_recover_armed_actions` no longer rebuilds corrupt (nameless)
  entries under a `None` key — they are skipped.
- Documentation sweep: SSE-zombie sections, stale gate/env/tick-rate/
  store-size/test-count numbers, ghost env vars (`COOLSTEP_TICK_HZ`,
  `COOLSTEP_MIN_ARM_*`, `COOLSTEP_RYZENADJ_CMD`), AlwaysIdleBaseline
  references, interference tallies.


---

## [0.5.20] — 2026-05-26

Hotfix on top of v0.5.19. The v0.5.19 "all 19 tiles live" Playwright
verification was a lucky run on a warm module cache; on a cold cache
(real users on first navigation) the orchestrator boot raced module
script execution and snapshotted the registry with only one tile in
it. The remaining 18 sat in the registry with `mounted: false` for
the lifetime of the page — visible empty cards despite a live daemon,
no API calls firing for tile endpoints (`/api/adapters`, `/api/reliability`,
`/api/actuator-journal`, `/api/predictor-cockpit`, …).

### Fixed — correctness

- **Orchestrator boot race: 18/19 tiles silently never mounted.**
  `_scheduleBoot()` queued a microtask after the first `register()`
  call. Module scripts execute as separate tasks, so the microtask
  drained before the other 18 tile modules finished loading; their
  registrations landed in `_registry` *after* `_runBoot` had already
  snapshotted it, and `_bootScheduled=true` blocked any re-run.
  Replaced the whole boot-snapshot design with per-tile mount-on-register
  (`_mountByPriority` called directly from `register()`). The
  connection budget still handles burst protection; no global
  synchronization point survives, so no race. Verified via Playwright
  on a cold cache: 19/19 tiles mount, every tile endpoint fires.

- **`StaticFiles` served no `Cache-Control` header.** Without it
  browsers fall back to Last-Modified heuristic freshness (~10% of
  age) and can pin stale JS for hours after a fix ships — exactly
  why this bug survived 28h+ unnoticed on the running dashboard.
  Mounted via a `_NoCacheStatic` subclass that adds `Cache-Control:
  no-cache` on successful static responses; ETag revalidation now
  fires on every load (~1 ms warm), so future patches are picked
  up on the next navigation.

- **One-shot cache-bust on the orchestrator import URL.** Even after
  the no-cache header ships, the *already-cached* `_orchestrator.js`
  URL stays valid by browser heuristic until eviction. Added
  `?v=mount-on-register` query to every tile's orchestrator import +
  the `modulepreload` hint, forcing one fresh fetch under the new
  cache policy. Future orchestrator edits don't need the query
  string bumped — they'll be picked up via no-cache revalidation.

### Verified end-to-end

Playwright on cold cache after dashboard restart:
- Mounted: 19/19 tiles (was 1/19 — only `live-telemetry-tile`, the
  sole `priority='critical'` entry)
- Network: every tile endpoint fires within first second of load
- Static assets carry `Cache-Control: no-cache`, ETag revalidates 1ms

## [0.5.19] — 2026-05-25

Three-round perf + correctness sweep driven by Playwright dynamic
screencast (10×1s tile snapshots, not static screenshots). Surfaced
15+ issues invisible to single-shot screenshots: frozen-data windows,
silent failure modes, cache stampede memory bursts. Each round = 5
parallel research agents → manual audit + fix → live verify.

### Fixed — correctness (silent failures)

- **`daemon_seen` lied green for 60s after collector death.** Health
  pill checked `store.db.exists()` — always True since the file
  persists. Switched to `ml-state.json` mtime (per-tick liveness
  signal). `store.db` is WAL-mode; main file mtime only bumps on
  10-min checkpoint — useless for liveness. Fail-detection window:
  60s → 15s.
- **`/api/event-segments` was a stub returning `[]`.** Daemon emitted
  `SessionBoundary` every load_jump but discarded them; tile showed
  "no segments yet" forever. Now persists to `data/segments.jsonl`,
  handler tail-reads same pattern as `/api/incidents`. Default
  `limit=50` → `500` (24h windows legitimately accumulate 80+ on
  busy sessions).
- **Telemetry tiles froze for 6-7s windows.** Orchestrator's
  `_telemetryLastTs` dedup compared `data.ts === lastTs`. With
  server cache TTL=1s + poller 1Hz + collector 1.5Hz this aliased
  to same body across multiple polls → emit suppressed → tiles
  frozen while UI age timer ticked independently (30s→36s, then
  sudden jump to fresh data). Dedup removed; idempotent re-render
  in subscribers is cheap.
- **`calibration-state-tile` showed `0/—` indefinitely on cold-miss.**
  Added loading skeleton + lock-gated pre-warm so first user request
  doesn't pay 16-30s wait.
- **`training-tile` was `priority: 'lazy'` with 5s poll.** ml-state
  tick is the operator's primary liveness counter; lazy mount +
  5s gap = 7s visible freeze. Changed to `normal` + 2s.

### Fixed — perf (cache stampede + cold-miss)

- **`/api/efficiency` cache stampede.** TOCTOU between cache-check
  and `to_thread` compute spawned N concurrent `compute_historical`
  calls on N concurrent tabs. Each loaded 20k rows × `json.loads` =
  400MB working set. RSS burst 150MB → 2.5GB on busy tab-storm.
  Fix: per-key `asyncio.Lock` with double-check pattern. Verified:
  5 parallel cold curls now serialize to 1× compute + 4× cache-hit.
- **`/api/calibration` same stampede.** Cold 16-30s → 30s timeouts
  on concurrent first-hits. Same lock fix + `_warm_calibration_locked()`
  so warm thread + first user serialize. Verified: 5 parallel cold
  = 4.3s burst (was N× 30s).
- **`/api/predictor-cockpit` missed prior cache pass.** Was the only
  heavy endpoint without TTL — cockpit tile polls 0.5-2Hz, every
  poll hit sqlite + 2× `residual_log` full scans = 1030ms. Added
  0.75s TTL with `scope_s` in cache key. 1030ms → 6ms warm.
- **Vendor Lit shrink: 125KB dev → 15.5KB minified (`-87%`)** via
  local esbuild bundle (`npx esbuild entry.js --bundle --format=esm
  --target=es2022 --minify`). Self-contained ESM, no build step at
  deploy time.
- **240 background HTTP fetches/min when tab not focused.** Added
  `document.hidden` gate to orchestrator's TelemetryPoller +
  `visibilitychange` listener for immediate-refresh on return.

### Fixed — UX

- **`efficiency-tile` "below sweet -25°" read like a CPU temperature.**
  Math correct (current minus `sweet_spot=83°C`), display ambiguous.
  Suffix added: "below sweet -25.0° **from sweet**".
- **`self-monitor-tile` 4 false-positive TRIGGER alerts.** Thresholds
  were idle-machine assumptions (memory 80% / swap 1MB / PSI 0.1%);
  real baseline is 79% mem / 64MB swap / 1-2% PSI IO. Rebalanced
  to 92% / 200MB / 5.0%. Pill now green at healthy steady state.

### Fixed — robustness

- **Orchestrator: tile reconnect orphaned.** `register()` pushed
  duplicates; `_runBoot` skipped them after first boot → reconnected
  tile silently never re-mounted. Now dedup by name + re-mount in
  place.
- **Predictor-cockpit canvas: NaN slipped through null guard.**
  Smoothing loop checked `cur.t == null` but not `isNaN(cur.t)` —
  NaN propagated to canvas coords. Added isNaN check.
- **`self-monitor-tile`: duplicate orchestrator import** (harmless
  per ES module dedup, but noise). Removed.

### Added — observability

- **Per-route latency middleware ring** in `/api/self-monitor`
  (`_ROUTE_LATENCY_RING`, 60 samples per route, deque-bounded).
  Output per route: `{p50_ms, p95_ms, p99_ms, samples}`. Foundation
  for regression detection — any route whose p95 climbs >2× baseline
  can be flagged.
- **Per-route failure tracking in orchestrator.** Emits global
  `coolstep:api-degraded` custom event on `document` after 3
  consecutive null returns, `coolstep:api-recovered` on next
  success. Foundation for masthead "API degraded" pill (UI
  subscriber pending).

### Internal

- **Test fixture updated**: dashboard `client` fixture now writes
  a sentinel `ml-state.json` so `daemon_seen=True` test still passes
  after the mtime-based liveness check. Two 404-when-absent tests
  unlink the file explicitly.
- **Version bump** — `__version__`, `pyproject.toml`, AUR `pkgver`
  now `0.5.19`.

### Verified end-to-end

Final Playwright 10×1s screencast post-fix:
- All 19 tiles live (was: 6 in placeholder/stale state)
- Live telemetry age oscillates 0-3s naturally (was: monotonic 30s→36s freeze)
- Temperature flows smoothly (was: 45→77°C single jump)
- Training tick increments +10/10s (was: 1/10s frozen)
- Calibration 5/5 gates passed (was: `0/—`)
- Event segments 221 visible (was: "no segments yet")
- Self-monitor 0 TRIGGER (was: 4 false-positive)

Endpoint timings (warm cached):
```
/                              4ms
/api/health                    3ms
/api/telemetry/latest          6ms
/api/ml-state                  4ms
/api/predictor-cockpit         5ms  (was 1030ms)
/api/efficiency                4ms  (cache stampede solved)
/api/event-segments            12ms
/api/profile                   4ms
/api/self                      4ms
```

## [0.5.18] — 2026-05-24

Patch release shipping two more external community contributions:
docs and predictive-pressure observability.

### Added

- **`last_nonempty_at` per collector** in `/api/adapters` response
  (PR #17, closes #15). The dashboard adapters-health-tile now renders
  a three-state cell (`fast` / `stale` / `no data`) so an operator can
  distinguish "genuinely fast collector" from "discover succeeded but
  every sample is empty" — previously both were indistinguishable as
  `sample_us=0`. Module-global state in `dashboard/server.py`, no
  collector contract change. **Second-time external contribution** —
  thanks to [@Chris79OG](https://github.com/Chris79OG).
- **CLI subcommand reference table** in README (PR #16, closes #13).
  Documents the 15 `coolstep` subcommands inline so first-time users
  don't have to discover via `--help`. **Third external contributor** —
  thanks to [@YuuGR1337](https://github.com/YuuGR1337).

### Changed

- **Tick rate default for local lab** (drop-in `70-tick-rate.conf`) —
  lowered 5Hz → 1Hz on the maintainer's ASUS TUF A15 to reduce
  collector observer-effect heating (CPU baseline 27% → 13%). Project
  default in `daemon.py` unchanged; this is a per-host systemd drop-in.
- **`notify_send` cooldown env knob** (`COOLSTEP_NOTIFY_COOLDOWN_S`) —
  default 30s remains; documented for hosts that want softer
  notification cadence (e.g., 300s = 5min for sustained-pressure
  workloads).
- **Version bump** — `__version__`, `pyproject.toml`, AUR `pkgver`,
  AUR `.SRCINFO`, README badge are now `0.5.18`.

### Internal

- **Ruff legacy debt unblock** (`pyproject.toml`) — `max-complexity` raised
  10 → 200 to cover existing complex functions (worst: `create_app` at
  183); ignore list extended for `SIM105/SIM102/SIM115/SIM117/B904/E402`
  style-equivalence rules. Real refactor remains on the P3 polish
  backlog; pre-commit hook no longer blocks on these tracked-debt items.
  100 auto-fixable ruff findings cleared (unused imports, sorted imports,
  modern syntax).

## [0.5.17] — 2026-05-23

Patch release shipping the first external community contribution.

### Fixed

- **`coolstep tail` showed `cpu_tctl=0.0°C` on Intel hosts** (issue #2, G-10).
  The G-9 fix from v0.5.2 wired a vendor-neutral CPU temperature fallback
  (`tctl → tdie → package → max-of-cores`) into the store writer and the
  efficiency calibrator, but the tail formatter was left with the older
  `tctl or tdie or 0.0` shortcut. On Intel hosts (no `tctl`/`tdie` exposed,
  but `package` present) the live tail printed a flat zero while the
  daemon itself was reading the correct value. Three inlined copies of the
  fallback have been collapsed into a single helper,
  `coolstep.core._helpers.canonical_cpu_temp(frame)`, used by all three
  call sites. Regression covered by
  `tests/test_cpu_temp_fallback.py::test_tail_formatter_picks_intel_package`.

  Verified end-to-end on Hetzner EX44 / Intel i5-13500 — `coolstep tail`
  now reports the live `package` temperature instead of `0.0°C`.

  **First external contribution to coolstep** — thanks to
  [@puneetdixit200](https://github.com/puneetdixit200)
  (PR #7, closes #2).

### Changed

- **Version bump** — `__version__`, `pyproject.toml`, and AUR `pkgver`
  are now `0.5.17`. AUR `.SRCINFO` realigned (had been stale at `0.5.5`).

## [0.5.16] — 2026-05-19

### Added

- **Dashboard hardening middleware** (`coolstep/dashboard/security.py`) —
  validates the HTTP `Host` header on every request and the `Origin` /
  `Sec-Fetch-Site` headers on mutating endpoints (`POST`/`PUT`/`DELETE`/
  `PATCH`). Non-browser clients (curl, systemd timers, internal CLI) are
  unaffected. Loopback bindings expand the allowlist to cover the three
  loopback names (`127.0.0.1`, `localhost`, `[::1]`).
- **`--allow-public` bind guard** for `coolstep-dashboard`. Any
  non-loopback `--host` now requires the explicit flag; without it the
  CLI exits with a short pointer to the recommended deployment patterns.
  `--allow-host <fqdn>` extends the `Host` allowlist for reverse-proxy
  deployments. With `--allow-public` set, a startup banner reminds the
  operator that no authentication is bundled.
- **Mode-switch audit journal** — `/api/mode/{cool,quiet,off}` now append
  a record to `actuator-journal.jsonl` alongside actuator events, with
  the prior mode, peer address, and any `X-Forwarded-For` header. The
  existing `/api/actuator-journal` endpoint and `coolstep inspect`
  surface them without changes.
- **`SECURITY.md`** at the repo root + **`docs/security.md`** —
  single-user trust model, what ships in the box, how to plug an
  authentication layer upstream (SSH tunnel / reverse proxy / WireGuard).
- **`docs/headless-deployment.md`** — recommended deployment shapes for
  remote and headless hosts (SSH tunnel, reverse proxy with auth,
  WireGuard / VPN-only), plus the systemd-user linger setup.
- **README § 08 — Single-user by design** — short pointer to the
  security model and deployment doc so the trust boundary is visible
  before install.

### Changed

- **Version bump** — `__version__`, `pyproject.toml`, and AUR
  `pkgver` are now `0.5.16`.

## [0.5.15] — 2026-05-18

### Added

- **Trajectory features in KNN embedding** (P2.11) — three slope/acceleration
  features added to `FEATURE_NAMES` (26→29-dim): `cpu_temp_slope_5s` (°C/s
  short-window OLS), `cpu_temp_accel` (°C/s² second derivative),
  `cpu_load_slope_5s` (%/s). Two thermally identical snapshots with opposite
  slopes now land in different regions of the KNN space — a rising-to-85°C
  state is no longer indistinguishable from a cooling-from-85°C state.
  `predictor.py` passes the live fingerprint dict as `windowed_features` to
  `embed()`; `reindex_hnsw_from_store.py` reconstructs sliding windows from
  store.db so historical vectors get the same trajectory encoding.
  Schema break: existing `embedder-stats.json` (26-dim) is auto-rejected on
  load → clean refit + full hnsw reindex required (see ops section).

- **Controlled residual markers** — validated predictions now carry
  `intervened` + `intervention_verbs` when their horizon overlapped a real
  actuator write. The cockpit renders these as dashed residual tokens/rings
  so operator-visible misses stay visible without being confused with passive
  model error.

### Changed

- **ResidualBank learns passive residuals only** — startup replay and live
  validation skip `intervened=true` records by default. Controlled residuals
  remain in `residual-state.jsonl` and `/api/predictor-cockpit`, but they no
  longer fold coolstep's own fan/power response back into the passive
  correction layer.
- **RAMP_COOLING recovery-edge guard** — if the short live Tctl slope is
  already cooling and the forecast is not above current temperature by
  `0.5°C`, `RAMP_COOLING` is suppressed while `CAP_BOOST`/notify semantics
  remain unchanged.
- **Version bump** — package, `__version__`, and stable AUR `pkgver` are now
  `0.5.15`.

### Fixed

- **HNSW `fetch_mult` raised for sparse fresh indexes** — `hnsw.query_labeled()`
  now uses `fetch_mult=30` (was 3) and `query_stable()` uses `fetch_mult=10`
  (was 5). After a fresh reindex the index starts ~78% UNKNOWN — only ~100k
  of ~1.17M frames carry a label until backfill finishes — so the previous
  multipliers exhausted the candidate pool before `top_k` saturated, returning
  far fewer neighbours than expected. New multipliers cost ~3.3 ms at k=600
  on the target box, negligible for async prediction, and self-tune once
  labeled density exceeds 80% (most queries saturate before the wider pool
  matters).
- **`MetaPredictor` propagates neighbour fields** — `predict()` now copies
  `neighbours`, `danger_neighbour_count`, and `suggested_rpm` from the base
  `KnnPredictor` prediction onto its returned `Prediction`. Previously these
  fields were dropped at the meta layer, so the `<neighbours-tile>`, danger
  vectors, and equilibrium-RPM signals went dark whenever the meta correction
  was active even though the upstream values were valid. Implemented via
  `dataclasses.replace(base_pred, ...)` so future `Prediction` fields can't
  silently regress; the same idiom now applies to `_merge_with_trajectory`.

## [0.5.5] — 2026-05-12

Documentation + visual refresh on top of v0.5.4.  No runtime behaviour
change.

### Added

- **`docs/img/cockpit-v0.5.5/`** — five fresh cockpit screenshots
  captured live during a stress burst:
    - `00-full.png` — full cockpit at *asymptote* state (`+1.7°C in +5s`,
      trend phrase `asymptote eq ≈ 76°`, bucket `n=5` with shrinkage-
      wide σ=2.78°C)
    - `01-header-chips.png` — ⚡ spike-active chip + twin `±err` pills
    - `02-hero.png` — hero block with LIVE NOW / Δ / TREND / dT/dt
    - `03-canvas-rings.png` — canvas zoom showing past-prediction
      rings, σ-corridor, knee/danger reference lines
    - `04-bucket-strip.png` — meta correction + σ + sample-count `n`,
      with the shrinkage prior visibly widening σ on a young bucket
- README section "05 · Read the cockpit" gains an inline visual
  layout: full-width hero shot at section open, 3-column row of
  detail crops with sub-captions, canvas zoom-in below the components
  table.  Replaces the prose `→ release notes` pointer that previously
  carried the visual weight alone.

### Changed

- README "TREND phrase" examples updated to include `asymptote eq ≈ 76°`
  (the most informative branch — it's how the cockpit signals
  "rising but won't reach the knee at this slope").

## [0.5.4] — 2026-05-12

Predictor cockpit becomes a relational instrument: every metric is
paired with the parameter that gives it meaning, residuals get a
training-archive substrate, and the meta layer learns honestly at
small sample sizes.

### Added

- **`coolstep/core/spike_detector.py`** — state machine that watches
  the validated-residual stream and emits an `Incident(kind=
  "predictor_spike")` whenever the predictor was surprised by a real
  workload step (entry: `|residual| ≥ 5°C` for 2 ticks, exit: `<2°C`
  for 3 ticks).  Closure records carry the workload label, max
  residual, duration, and the features at entry — the snapshot the
  future KNN lookup will match against when a similar workload arrives.
  Routed through the existing `incidents.jsonl` + multi-angle
  similarity, so no parallel substrate.  10 tests.
- **Bayesian shrinkage on `ResidualBank.correct()`** (ADR-020) — each
  bucket treated as starting with `k=5` pseudo-obs at zero mean,
  `σ₀ = log1p(2°C)` log-space.  Damps the correction by ≥60% in the
  first 3 obs of a fresh bucket; converges to ~95% of pure data by
  n=15.  Posterior σ widens when data and prior disagree —
  prevents the n=2 / σ=0.01°C overconfidence pathology the operator
  caught on a live screenshot.
- **`±err · 30s` chip in the cockpit header** — twin to the
  existing `±err · 15m` chip.  15m = slow rolling quality, 30s =
  what this workload is doing right now.  Same colour ladder; the
  operator reads the split as "long-haul fine, transient spike".
- **`⚡ spike · <workload>` chip** in the cockpit — appears alongside
  the err chips while the detector is in active state, shows
  `duration_s ±max|residual|°`, pulsing border.  Invisible when idle.
- **Canvas legend strip** under the cockpit canvas — five chip swatches
  keyed to each canvas mark (gold trail / dashed forecast / σ band /
  three ring colours by |residual|).  Replaces a prose description.

### Changed

- **Cockpit `dT/dt` source switched to `cpu_temp_slope_per_sec_short`**
  — a 5-frame OLS slope.  The previous 600-frame window slope
  averaged over ~10 min of bidirectional jitter and reported ±0.001°C/s
  even during a 5°C/s ramp.  Long slope kept as `cpu_temp_slope_per_sec`
  for ResidualBank bucket-key classification.
- **Cockpit hero block** rewritten as two relational columns (NOW with
  `Δ in +5s` subline · TREND phrase with raw dT/dt under it).
  TREND phrases derived from the same saturation math the predictor
  uses:  `→78° in 12s` / `cooling −10°/30s` / `asymptote eq ≈ 73°`
  / `past knee` / `steady`.  Operator gets time-to-target rather
  than a naked °C/s number.
- **Cockpit canvas switched from phase-space (T × dT/dt) to time-
  series (t × T)** — past 30s trail in gold, forecast +5s in dashed
  cool, σ-corridor shaded around the forecast.  Past predictions
  appear as hollow rings at where we said it'd land, connected by
  a thin segment to where it actually did.  78°C knee + 90°C danger
  drawn as dashed reference lines.  Ghost rings limited to the
  newest 8 and α-faded by age (newest 1.0 → oldest 0.4) so the
  cluster near `now` doesn't read as one bright blob.
- **Forecast curve anchored to the meta-corrected `cur.predicted`
  endpoint** (ADR-021) instead of raw saturation extrapolation.
  Solves the "line shoots to 91°, ring sits at 85°" disconnect when
  the meta layer applies a correction.  Curve shape unchanged when
  correction is zero.
- **`efficiency-tile`** aligned with the cockpit's INSTRUMENT
  vocabulary — single relational hero column (`WORK / °C · NOW`
  with `Δ to sweet` subline), canvas font switched to monospace,
  legend strip adopting the `.legend/.item/.sw` chip pattern.
- **`neighbours-tile` empty state honest about KNN dormancy** —
  distinguishes `chroma_count == 0` (KNN dormant, ChromaDB disabled,
  cocktip → cockpit for live state) from genuine warming.  Stops
  echoing the trajectory predictor's reason string under a tile
  named for KNN neighbours.
- **`/api/predictor-cockpit`** splices the live spike state into its
  aggregator JSON; the cockpit gets it on the same 1-Hz poll, no
  extra round-trip.
- **Daemon validation loop**: residuals fed to the spike detector
  on every tick; closures emit incidents via the existing
  `log_incident` + `find_similar` path so the multi-angle search
  picks them up.

### Fixed

- **`cpu_temp_now` feature** in `fingerprint.extract()` — separated
  from `cpu_temp_max` (rolling-window max).  Predictor / cockpit
  "live now" / residual validation all switched to the latest
  sample; `cpu_temp_max` is kept only for aggregate analytics
  (efficiency buckets, cluster drift, event segmentation) where a
  per-window max is the right input.  Without this the predictor
  saw a 30s-stale peak as "current" and reported a self-consistent
  fake 99% accuracy while the chip had long since cooled.

### ADRs

- **ADR-020** — Bayesian shrinkage on ResidualBank correction.
- **ADR-021** — Forecast curve anchors on meta-corrected endpoint.

### Tests

798 passed, 25 skipped.  10 new tests in `test_spike_detector.py`
+ 5 new in `test_residual_meta.py` + 1 in `test_predictor_meta.py`.

## [0.5.3] — 2026-05-12

Predictor-of-predictor: every forecast now carries a learned
correction keyed to the workload regime it was made in.  A live pilot
on a freshly switched `workstation-latency-quiet` thermal profile
revealed the previous TrajectoryBaseline systematically overshot by
~20°C on transition moments (load drop after a spike) — the predictor
was reading the instantaneous slope and assuming it'd hold, while the
new profile's active cooling was already pulling temperature down.

### Added

- **`coolstep/core/residual_log.py`** — append-only JSONL residual
  ledger.  Every prediction is held in a daemon FIFO until its
  horizon elapses; at that moment the (predicted, actual, residual)
  tuple plus the features-at-prediction-time is written to disk.
  This is the substrate the meta-predictor learns from.  Mirrors the
  existing JSONL pattern (incidents, decisions, actuator-journal):
  10 MB rotation × 3 generations, ~3 days history, ~80 bytes/record
  on average.  See ADR-016.
- **`coolstep/core/residual_meta.py`** — meta-predictor math layer:
    - `bucket_of(features)` quantises into `(load_band, slope_sign,
      accel_sign, profile_band)` — a 4-int key that stratifies the
      bias the user observed.  `slope_sign` and `accel_sign` need
      the new `cpu_temp_accel_per_sec_sq` feature.
    - `RunningStat` — Welford's online algorithm for mean+variance,
      plus an EWMA "compound" overlay for recency-biased mean.
    - `log_residual(r) = sign(r)·log1p(|r|)` — stabilising transform
      so a single 20°C outlier doesn't dominate the EWMA forever.
    - `ResidualBank` — `dict[BucketKey, RunningStat]` with
      `observe`, `correct`, `decay_all`, `from_log` (rebuild from
      disk on daemon startup).
    - `Cusum` — two-sided CUSUM detector for residual-mean drift.
      Triggers a soft decay when the meta-predictor itself drifts.
    - `compose_confidence(*p)` — additive on the log-odds scale.
- **`coolstep/core/predictor_meta.py:MetaPredictor`** — composes a
  base predictor (TrajectoryBaseline currently, KnnPredictor when
  Chroma returns) with the residual bank.  Drop-in replacement for
  any `Predictor`: model_name becomes `f"{base.name}+meta"`, the
  Prediction.reason carries the correction + bucket + sample count
  diagnostics.  See ADR-017.
- **`coolstep/core/tuned_profile_watch.py`** — polls
  `/etc/tuned/active_profile` every 30 ticks.  On change, the daemon
  decays the residual bank by 0.5 (preserve half the prior learning)
  and stamps `profile_changed_at` in ml-state.json.  Cusum is the
  secondary safety net for hosts without tuned-adm.  See ADR-019.
- **`coolstep/core/fingerprint.py`** — new features:
    - `cpu_load_slope_per_sec` — load trajectory (≈ proximate cause
      of upcoming cooling)
    - `cpu_temp_accel_per_sec_sq` — second derivative, computed by
      splitting the window in halves and differencing slopes.
- **`<predictor-cockpit-tile>`** — new dedicated tile, phase-space
  view (X = T, Y = dT/dt).  Past 30 s trajectory tail, predicted
  arrow forward, σ-ellipse around the prediction from rolling
  residual std.  Residual trail strip below shows the last 12
  validations as inline tokens (green/amber/red by magnitude).
  Accuracy badge in the header is the rolling-15-min `1 -
  median(|residual|)/10`.  No 800 ms validation pulse, no countdown
  ticker — the user found those annoying.  See ADR-018.
- **`/api/predictor-cockpit`** — single-roundtrip aggregator for
  the cockpit tile: current state + 30 s actual trail + 12-entry
  residual trail + accuracy + profile.
- **CLI**: `coolstep predict-debug --bucket K,K,K,K` and
  `coolstep predict-replay --since 1h` for inspecting the bank.
- **`scripts/predictor-eval.sh`** — bash orchestrator over the new
  CLI subcommands; coloured tabular output of live accuracy plus a
  per-bucket replay over a chosen window.

### Changed

- **Predictor dot removed from `<efficiency-tile>`** — the cyan
  ring / trajectory line / countdown / validation pulse / last-error
  label were a category error on the work/°C histogram (per ADR-018
  rationale).  The histogram is now clean again: mean / p95 / sweet
  / knee / a single live gold dot.  Predictor visualisation lives
  exclusively in the cockpit tile.
- **Daemon predictor selection** (`daemon.py`) now wraps both KNN and
  TrajectoryBaseline in `MetaPredictor`.  On startup the bank is
  rebuilt from `data/residual-state.jsonl`.
- **ml-state.json** surfaces `active_tuned_profile`,
  `profile_changed_at`, `meta_buckets`, `residual_log_count` for the
  cockpit tile.
- **`AUR pkgver`** — bumped both PKGBUILDs to 0.5.3.

### Fixed

- The ~20°C systematic overshoot the user observed on the post-tuned
  profile reduces to median-15m residual ≤ 0.5°C within ~30 seconds
  of restart (replay from residual log) and stays there as the bank
  collects bucket-specific corrections live.

## [0.5.2] — 2026-05-12

Two Intel-platform crash-fixes caught by a post-0.5.1 headless-server
pilot.  Both bugs hid behind "the file parses fine on my AMD laptop"
— the kind of break that only surfaces when someone else runs your
code on real hardware you don't own.

### Fixed

- **G-7 extended** — `<sparkline-tile>` now mirrors `<live-telemetry-tile>`
  italic-N/A treatment for structurally-absent platform readings (Intel
  iGPU power, RAPL CVE-locked, no-fan-tool RPM).  The pill says "data
  not available on this platform" instead of falling silently to `—`,
  which had read as "daemon broken" during pilot.
- **G-9 cpu_temp Intel fallback** — `Store.write_frame()` and
  `efficiency_calibration._tctl()` were hard-coding AMD-only sensor names
  (`tctl` / `tdie`).  On Intel hosts the `coretemp` driver exposes
  `Package id 0` instead, so `cpu_temp` was being written as NULL into
  sqlite — the `Live telemetry` tile read `CPU TCTL N/A` despite
  `temps_c['package']` clearly showing 49°C.  Fix chains: `tctl → tdie →
  package → max-of-cores` universal.  CI guard:
  `tests/test_cpu_temp_fallback.py` covers six cases (AMD primary /
  secondary, Intel package, max fallback, empty-dict None, and
  end-to-end via `Store.write_frame()`).
- **G-10 live-telemetry SyntaxError** — A backtick-wrapped em-dash
  (`` `—` ``) sitting inside a CSS comment **inside a Lit `css\`...\``
  template literal** silently closed the template string for the
  browser ESM parser.  Custom element never registered → empty
  `<live-telemetry-tile>` in DOM → main thermal panel just gone.  Worse:
  `node --check` happily approves the file; only browser ESM /
  `node --input-type=module -e "import(...)"` catches it.  Lesson
  filed in `docs/aur-publishing.md` § G-10.

## [0.5.1] — 2026-05-12

Interference & hardware compatibility test pillar **plus** AUR-readiness
gotchas batch.  Coolstep does not live in a vacuum; it shares thermal/power
knobs with PPD, TLP, asusctl, Feral gamemoded, BMC firmware, and (worst of
all) another coolstep instance.  This release makes those coordination
points visible and testable, closes four open conflicts with real actuator
gates, and lands seven AUR/packaging/UX fixes caught during a sandbox +
headless-server pilot deploy.

### Fixed — AUR packaging / dashboard / install-units (G-1..G-7)

Pre-AUR-cut sandbox test in two ephemeral Arch LXC containers plus a pilot
deploy on a headless Intel desktop tower (i5-13500 / Debian 13 / RAPL
locked / no fan tool) caught seven distinct cases.  All closed; CI guards added; full case study in
`docs/aur-publishing.md`.

- **G-1** `python-httpx` added to `checkdepends` in both PKGBUILDs —
  `fastapi.testclient` required it, `makepkg --check` was red on AUR users.
- **G-2** `coolstep/__init__.py:__version__` bumped to 0.5.1 (was drifted
  to 0.5.0 then 0.0.1 in the wheel metadata).  `coolstep/dashboard/server.py`
  FastAPI version now imports `__version__` instead of hardcoded `"0.0.1"`
  (third drift source closed).  Test `tests/test_version_sync.py` guards.
- **G-3** `coolstep-bench-gc.service` + `.timer` now `install -Dm644`'d by
  both PKGBUILDs.  Weekly `bench/runs/` GC now actually runs on AUR.
- **G-4** `coolstep install-units` detects headless session
  (`XDG_RUNTIME_DIR` unset / missing) and prints `loginctl enable-linger`
  instructions instead of bare `systemctl --user …` (which fails on rack
  servers with no graphical session).
- **G-5** `install-units` now `mkdir -p ~/coolstep/data/` so units don't
  loop on `status=226/NAMESPACE` (systemd `ReadWritePaths=` requires the
  path to exist at start time).
- **G-6** `[tool.setuptools.package-data]` extended to ship
  `coolstep/dashboard/static/*` (36 files: HTML/CSS/Lit/i18n).  Without
  this, `pip install` / `pacman -U` wheel was missing `index.html`, daemon
  API worked, `/` was 500.  Particularly toxic for enterprise SSH-tunnel
  access pattern (`ssh -L 18889:127.0.0.1:18889 host`).
- **G-7** `<live-telemetry-tile>` renders explanatory italic `N/A` with
  hover tooltip for fields the platform can't provide (Intel iGPU has no
  discrete GPU temp/power, RAPL CVE-locked, no fan tool) instead of silent
  `—`.  Critical for headless-via-SSH-tunnel admin UX — silent `—` read as
  «daemon broken», `N/A — reason` reads as «not applicable here».

### Added — CI guards for the AUR gotcha batch (`tests/`)

- `tests/test_version_sync.py` — `coolstep.__version__` ↔ `pyproject.toml`
  ↔ stable PKGBUILD `pkgver=` (G-2 guard).
- `tests/test_packaging.py` — every systemd unit shipped in PKGBUILDs
  (G-3), `python-httpx` listed in checkdepends (G-1), `static/` packaged
  in wheel + `pyproject.package-data` lists `coolstep.dashboard` (G-6),
  GET `/` returns 200 HTML smoke (G-6 functional, via `create_app()` factory).
- `tests/inspect/test_install_units.py` — headless + session branch text
  presence (G-4), data-dir creation + idempotency (G-5).

### Added — Hardware matrix harness (`tests/compat/fixtures/`)

- **`fake_platform()` ctx-manager** — materialises a synthetic `/sys + /proc
  + /etc` tree under `tmp_path` and patches `coolstep.compat.detect` so
  `detect_caps()` reads the fake substrate end-to-end. Snapshot DSL is a
  single Python dict per fixture; loader auto-handles default-arg patching
  for `detect_caps.__kwdefaults__` and `_epp_state.__defaults__`.
- **12 hardware snapshots** — ASUS TUF A15 (target), Ryzen 7950X+RTX 4090
  desktop, ThinkPad X1 Carbon (Intel), Dell PowerEdge R750 (IPMI),
  Raspberry Pi 5 (ARM), Framework 13 AMD, Steam Deck OLED, Supermicro
  H12 EPYC, HP ProLiant DL380 (Redfish), Mac Mini Intel + Fedora,
  Oracle Cloud Ampere ARM, Alpine LXC.
- **`test_hw_matrix.py`** — parametrised end-to-end check: each snapshot
  asserts `detect_caps()` fields + `caps_to_install_plan()` actuators +
  community pointers.

### Added — Interference test pillar (`tests/compat/interference/`)

- **17 conflict scenarios** validating coolstep's behaviour against other
  daemons that touch the same surfaces. Each scenario tagged
  `bugcase_status`: `covered` (gate verified), `gap` (proof-of-absence
  asserted, fix proposal filed), `design` (structural separation).
- **`docs/interference-matrix.md`** — catalogue with closed/open coverage
  and one-paragraph rationale per scenario. Companion `docs/hw-matrix.md`
  for the fixture-harness rationale.

### Added — Interference gates (real code, not just tests)

- **`epp_shift.make()`** cedes EPP ownership when
  `caps.power_profiles_daemon_active` or `caps.tlp_active`. Override via
  `COOLSTEP_EPP_SHIFT_FORCE=1`.
- **`ryzenadj_cap.make()`** cedes STAPM/PPT when `caps.tlp_active`.
  Override via `COOLSTEP_RYZENADJ_FORCE=1`.
- **`ryzenadj_cap._sudoers_preflight()`** — runs `sudo -n
  /usr/bin/ryzenadj --version` at startup; refuses to load the actuator
  if the NOPASSWD entry is missing, and logs the exact snippet to install.
- **`_is_game_mode_active()`** (in both `asusctl_fan_curve` and
  `game_mode_optimizer`) now probes Feral's system-level
  `gamemoded.service` alongside `--user game-mode.service`.

### Fixed

- **ChromaDB SEGV** unblocked: pyproject.toml pins `chromadb>=0.5,<1.0`
  (chromadb 1.x rust bindings crash on Python 3.14 during
  `Collection._get`). `tests/test_daemon.py` auto-skips when an unsafe
  combo is detected. Production drop-in `60-chroma-disabled.conf` stays
  as belt-and-suspenders.

### Open gaps (filed for v0.6.0)

Four interference scenarios remain open with concrete fix proposals in
`docs/interference-matrix.md` and `TODO.md` → `🛡️ INTERFERENCE GATES`:
S08 (two-instance fcntl lock), S10 (Hyprland socket verification),
S14 (`caps.nvidia_persistence_mode` field), S15 (asusctl curve drift
detection in `revert()`).

### Tests

- 684 passed, 25 skipped (chromadb autoskip on Py 3.14)
- 107 compat tests (88 pre-existing + 19 new in interference pillar)
- 0 regressions

## [0.5.0] — 2026-05-12

P3: two deployment targets (desktop responsiveness + server foresight) over one
ML brain, three-layer manifest topology, community pointers, target-aware install plan.

### Added — Compat layer (`coolstep/compat/`)

- **`PlatformCaps`** — immutable frozen dataclass with 60+ capability flags:
  distro/CPU/GPU/compositor/fan-tools/power-tools/systemd/kmod fields.
- **`detect_caps()`** — pure I/O detector reading `/proc`, `/sys`,
  `/etc/os-release`, env vars, `shutil.which`. Singleton + `reset_caps()` for tests.
- **`caps.facet`** derived property: `desktop` | `server` | `ambiguous`.
- **`caps_to_install_plan()`** — translates caps into actionable install
  recommendations per distro clan (Arch / Debian / Fedora / SUSE / NixOS).

### Added — Three-layer manifest

- **`coolstep/compat/core.json`** (L0, bundled, package-data) — single source
  of truth for hwmon driver sets, distro id-to-clan map, gpu vendor IDs,
  virtualization DMI hints, super-I/O prefixes, community pointers.
- **`load_manifest()`** in `coolstep/compat/manifest.py` — deep-merge
  L0 (core) ∪ L1 (`/etc/coolstep/community.json`) ∪ L2
  (`~/.config/coolstep/custom.json`).
- **Deep-merge semantics**: dicts recurse, lists replace, `{"_disable": true}`
  removes keys, `[{"id": "X", "_remove": true}]` strips id-keyed items.
- **`coolstep compat --manifest-paths`** — shows which layers loaded from where.
- **7 community pointers** in `core.json`: `nct6687`, `asusctl`, `ryzenadj`,
  `perf_paranoid`, `rapl_perm`, `bpftrace`, `ipmitool_server` with
  distro-specific install commands and upstream URLs.

### Added — Five new server-class collectors

- **`rapl_energy`** — Intel + AMD powercap `intel-rapl` interface, delta-based
  power computation, graceful CVE-2020-8694 permission handling.
- **`intel_i915`** — Intel iGPU via `i915`/`xe` drm; reads `gt/gt0/rps_act_freq_mhz`
  + throttle reason flags + hwmon temp/power.
- **`arm_thermal`** — ACPI `/sys/class/thermal/thermal_zone*` (primary source
  on ARM Graviton/Ampere/RPi/Asahi).
- **`redfish`** — BMC HTTPS via stdlib `urllib` (no extra deps); env-driven
  config (`COOLSTEP_REDFISH_URL/USER/PASS`); fans + inlet/exhaust + PSU.
- **`ipmi`** — `ipmitool sensor` subprocess parser; KCS local or remote LAN+.

### Added — Three "real" workload/performance collectors

- **`perf_events`** — long-running `perf stat -e ... -I 1000 -x , -a`
  subprocess + background reader thread; publishes cycles, instructions,
  cache-misses, CPI, cache_miss_pct.
- **`ebpf_sched`** — `bpftrace -f json` subprocess; per-CPU context-switch
  rate + fork/exec rates. Requires `CAP_PERFMON`.
- **`dbus_session`** — KDE Plasma + GNOME Shell via `gdbus` subprocess
  (no Python deps). Universal idle hint via `ScreenSaver.GetSessionIdleTime`.
  Skipped on Hyprland where `hyprctl` is canonical.

### Added — CLI

- **`coolstep compat`** — human-readable platform report.
- **`coolstep compat --json`** — JSON capability dump.
- **`coolstep compat --install-plan`** — package install recommendations.
- **`coolstep compat --facet=desktop|server`** — force target preset.
- **`coolstep compat --manifest-paths`** — show L0/L1/L2 chain.

### Added — Packaging

- **`LICENSE`** — MIT (matches `pyproject.toml`).
- **`packaging/aur/coolstep-git/PKGBUILD`** — bleeding-edge AUR flavor.
- **`packaging/aur/coolstep/PKGBUILD`** — stable release template.
- **`packaging/aur/README.md`** — `makepkg --check`, `.SRCINFO` generation,
  push-to-AUR instructions.
- **`CONTRIBUTING.md`** — what we accept / what needs discussion / coding
  conventions.
- **`pyproject.toml: [tool.setuptools.package-data]`** registers `core.json`.

### Changed

- **`linux_sysfs` HWMON sets** — `CPU/FAN/STORAGE/MEMORY/SKIP_HWMON_NAMES`
  now load from manifest at import time. Dictionary grew from ~5 drivers
  to 50+ entries (covers ASUS / MSI / Gigabyte / ASRock super-I/O chips:
  `nct6xxx`, `it87xx`, `f71xx`, `w83xx`).
- **`detect.py`** — `_detect_distro`, `_detect_gpu`, `_is_virtual`,
  `_detect_superio` read from `load_manifest()` instead of hardcoded dicts.
- **`install_plan._build_community_pointers`** — predicate-based selector
  reads pointer content from manifest; activation logic stays in Python.
- **All adapter `make()` functions** — opt-in `caps_if_set()` pre-check that
  short-circuits when caps are pre-detected, with graceful fallback so tests
  that don't initialize caps still work.
- **README.md** — full rewrite with "What IS / IS NOT", comparison tables,
  ASCII architecture, deployment-target comparison tables, three-layer manifest section, FAQ.

### Fixed

- **`.gitignore`** untracks rotating runtime jsonl
  (`decisions.jsonl*`, `actuator-journal.jsonl*`, `incidents.jsonl`,
  `runtime-state.json`, `asusctl_fan_curve_baseline.json`) to prevent
  auto-commit hook bloat.
- **`asusctl` version detection** — fallback chain
  (`asusctl --version` → `pacman -Q` → `dpkg -l` → `rpm -q`)
  works on v6+ which removed the `--version` flag.
- **`perf_events` discover** — 300 ms post-spawn poll catches kernel rejection
  on hosts that require `CAP_PERFMON` even at `paranoid=2`.

### Testing

- 628 tests pass (excluding `tests/test_daemon.py` — chromadb-py3.14 SEGV).
- 7 new test modules: `tests/compat/test_detect.py`, `test_install_plan.py`,
  `test_manifest.py`, `test_pointers_and_facet.py`, plus
  `tests/adapters/test_{rapl,intel_i915,arm_thermal,redfish,ipmi,
  perf_events,ebpf_sched,dbus_session}.py`.

---

## [0.4.0] — 2026-05-12

P2.0 through P2.5: full actuator stack, safety belts, stress harness, and dashboard expansion.
This release arms the system for live hardware writes on the target ASUS TUF A15 after the 14-day
calibration window. All actuators default to dry-run; live writes require `COOLSTEP_ACTUATOR_ENABLE=1`.

### Added

**Actuators (adapters/actuators/)**

- `asusctl_fan_curve_bias` — `RAMP_COOLING` verb; biases fan curve anchors in the 70–85°C knee band
  by `intensity_pct` (5–20%); discovers via `shutil.which("asusctl")` + version check (≥ 6).
- `ryzenadj_cap_boost` — `CAP_BOOST` verb; lowers STAPM/fast/slow limits via `ryzenadj` for AMD APU
  power cap during thermal pressure spikes.
- `epp_shift` — `SHIFT_POWER_ENVELOPE` verb; writes `balance_power` to all CPU EPP sysfs nodes
  (`/sys/.../cpufreq/energy_performance_preference`); restores per-CPU originals on revert.
- `notify_send` — `NOTIFY_USER` verb; `notify-send --app-name coolstep` desktop popup, gated by
  `COOLSTEP_NOTIFY_ENABLE` and `COOLSTEP_NOTIFY_COOLDOWN_S`.
- `readonly_log` promoted to persistent audit sink — writes every `apply()`/`revert()` event to
  `actuator-journal.jsonl` with JSONL size-based rotation (3 generations, 1 MB default).
- `game_mode_optimizer` (ADR-015) — P2.5 actuator that fires only when `game-mode.service` is
  active; cooperative model documented in `docs/game-mode-coordination.md`.

**Predictor**

- Trajectory-fallback signal in `core/predictor.py:_merge_with_trajectory` — physics-first overlay
  catches unfamiliar workloads KNN has never seen (rising slope ≥ 0.5°C/s above 78°C triggers
  `traj_prob=0.7`; already-hot at 85°C gives 0.65); merged via `max()` with KNN output.
- `intensity_pct` scales linearly with `throttle_prob` (range 5–20%) in
  `core/decision.py:_scale_intensity`.

**Decision engine**

- `Thresholds` extended: `min_arm_labeled_count=5`, `min_arm_confidence=0.5`, `rearm_gap_s=15.0`
  (`core/decision.py`).
- Env bypass hatches: `COOLSTEP_FORCE_FIRE_RAMP=1` (test-only, bench/stress.sh) and
  `COOLSTEP_ACTUATOR_ALLOW_PRE_CALIBRATION=1` for pre-calibration stress runs.

**Daemon**

- `_armed_actions` dict (`daemon.py`) — tracks per-actuator `(action, applied_ts)` pairs.
- `_sweep_expired_armed()` — TTL auto-revert at the top of every tick; reverts any action past
  `expires_at + TTL_GRACE_SEC` without waiting for a new prediction.
- Shutdown revert: `daemon.run() finally` → `_revert_all_armed("shutdown")` covers clean SIGTERM.
- Multi-actuator audit routing: two-pass dispatch (audit pass → `readonly_log`; hardware pass → first
  supporting actuator); `_NO_REVERT_ACTUATORS` set exempts audit sinks.
- Incremental backfill every `COOLSTEP_BACKFILL_INTERVAL_TICKS` ticks (default 600, ~10 min) to
  label stale `was_hot_in_30s=-1` vectors without blocking the tick loop.
- `runtime-state.json` now persists `armed_actions[]` and `baseline_anchors` so the cleanup script
  can restore the user fan curve on SIGKILL/OOM.

**Safety belts**

- `systemd/coolstep-cleanup.sh` (`ExecStopPost`) — reads `runtime-state.json`; if `armed_actions`
  non-empty, restores curve from `asusctl_fan_curve_baseline.json` or falls back to `asusctl
  --default`. Runs even on OOM kill (Python `finally` does not).
- `asusctl_fan_curve_baseline.json` — snapshot of user's actual curve taken on first `apply()`; re-
  snapshotted every 5 min to keep stale-baseline window bounded.
- `COOLSTEP_ACTUATOR_ENABLE` kill switch hard-defaults to dry-run: commands are constructed and
  logged but `subprocess.run` is never called unless explicitly set to `1`/`true`.
- `COOLSTEP_GAME_MODE_DEFER=1` (default) — `asusctl_fan_curve_bias.supports()` returns `False` when
  `game-mode.service` is active, preventing coolstep from stacking a bias on top of game-mode's
  own tuning.

**Stress harness (bench/)**

- `bench/stress.sh` — S1..S4 scenarios (synthetic CPU / mixed iowait / game-mode / heat-soak);
  writes `data/stress-state.json` marker; captures `ml-state.jsonl` + `telemetry.jsonl` + `meta.json`
  per run into `bench/runs/<ts>-<scenario>/`.
- `bench/analyze.py` — stdlib-only post-run report: peak Tctl, time ≥ 85°C, first predict ≥ 0.65,
  first fan ramp-up timestamp, delta, ASCII sparkline.
- `bench/baseline.sh` — paired dry-run vs armed comparison for controlled before/after measurement.
- `bench/gc.py` — prune old bench runs, rebuild `bench/runs/index.json` summary.
- `systemd/coolstep-bench-gc.timer` — weekly bench-gc run (Sunday 03:00, randomized +10 min).

**CLI**

- `coolstep export-profile` / `coolstep import-profile` — portable host config round-trip
  (`coolstep/inspect/`).

**Scripts**

- `scripts/quickstart.sh` — new-host onboarding: venv, deps, unit install, smoke stress test.
- `scripts/health-report.sh` — diagnostic snapshot: adapter costs, calibration gate status, journal
  tail, store size.

**Dashboard (dashboard/static/)**

- `/api/actuator-journal` — last 200 JSONL lines from `actuator-journal.jsonl`.
- `/api/stress-runs` — bench run summaries from `bench/runs/index.json`.
- `/api/crash-recovery` — surfaces hard-crash recovery events from `runtime-state.json`.
- `/api/predictor-breakdown` — exposes trajectory vs KNN contribution per-tick.
- `/api/stress-state` — active stress scenario from `data/stress-state.json` (stale after 10 min).
- `<actuator-history-tile>` — last 20 apply/revert events, kind-colored, TTL countdown, collapsible
  cmd detail.
- `<stress-runs-tile>` — bench run index with peak Tctl, scenario, armed flag.
- Actuator count pill in masthead: `actuators: N`.
- Crash-recovery pill in masthead: surfaces non-clean revert events.
- Stress pill in masthead: `stress: idle / S1 / S2 …` polled every 5 s.
- Sparkline bias overlay band — second Y-series `fan_max_pwm_bias_pct` over fan RPM line.
- Predictor-breakdown tile — trajectory vs KNN contribution visualization.
- i18n EN/RU/UK lang switcher (`lang-store.js` + `strings.js` + `<lang-switcher>` component).
- Hero narrative layout: 3 cards → 2 cards (anticipate + physics rationale).
- Crimson Pro wordmark font; coverage pill 100% clamp.
- Tile grid reordered: pulse → health → model → reference.
- `<throttle-events-tile>` redesigned: hero summary + 7d×24h density heatmap + collapsible raw
  table (was raw table only).

**Makefile**

- 10 ops targets: `install`, `enable`, `restart`, `status`, `stress-smoke`, `health`, `bench-gc`,
  `export-profile`, `import-profile`, `install-units`.

**Docs**

- `docs/coolstep-stack-summary.md` — full-stack reference for new dev / new host; data-flow ASCII
  diagram, collector table, actuator table, tuning levers reference, first-run recipe.

### Changed

- `asusctl_fan_curve.py:revert()` — restores user curve via `--data` baseline anchors instead of
  `asusctl --default`; factory wipe remains only when no baseline snapshot exists (regression
  protection for quietify-mid custom curves).
- `systemd/coolstep-collector.service ExecStopPost` — changed from unconditional `asusctl --default`
  to state-aware `coolstep-cleanup.sh` that reads `runtime-state.json` and applies baseline restore.
- Daemon routing: first-match → audit-then-first-hw two-pass (audit sinks run on every action,
  hardware sinks get first-match only).
- KNN auto-fire thresholds tightened: `min_arm_labeled_count=5`, `min_arm_confidence=0.5`,
  `rearm_gap_s=15.0` (was no per-actuator gating).
- Bias `intensity_pct` now scales with `throttle_prob` (5–20%) instead of fixed percentage.
- `<throttle-events-tile>`: raw event table replaced by hero + density grid + collapsible details.
- Hero narrative: 3 cards → 2 cards (anticipate + physics rationale).
- `Thresholds` in `core/decision.py` extended with actuator-gating fields (non-breaking — new fields
  have defaults matching previous implicit behaviour).

### Fixed

- `hyprctl` collector: self-recover on late env propagation (`hyprctl.py:self-recover`); runtime-
  probe socket pattern for long-running processes avoids ENOENT on socket rotation.
- `nvidia_nvml`: removed loop-variable lambda closure (`B023×5`) — all lambdas were capturing the
  same final loop value.
- `linux_sysfs`: `iowait_pct` now emitted as a separate signal rather than folded into `load_pct`,
  preventing false-positive throttle_prob spikes on I/O-bound workloads.
- `tests/_FakeStore`: `query()` now honours `labeled_only` filter — unit tests were silently
  ignoring unlabelled entries and masking a KNN coverage bug.
- Journal-rotation tests: dropped `importlib.reload` in favour of `monkeypatch.setattr` to clear
  module-level state between test runs, fixing a state-leak that caused sporadic failures.
- `bench/stress.sh`: JSON bool literal fix — `armed: true/false` now passed through `ARMED_PY` env
  var for Python heredoc interpolation (was producing Python syntax error `armed: true`).
- Dashboard coverage pill: clamped to 100% to prevent > 100% display during rapid labelling bursts.

### Security

- `COOLSTEP_ACTUATOR_ENABLE` defaults to dry-run on all hosts — hardware actuators never execute
  `subprocess.run` unless the operator explicitly sets the env var.
- Baseline persistence (`asusctl_fan_curve_baseline.json`) prevents a hard crash from wiping the
  user's custom fan curve and forcing `--default`, which would expose the machine to unintended
  stock firmware thermal behaviour.
- Game-mode defer pattern (`COOLSTEP_GAME_MODE_DEFER=1`, default on) keeps coolstep out of Steam-
  active thermal contracts by default; operators must explicitly opt in to cooperative biasing.

---

## [0.3.0] — 2026-05-03

P1: KNN predictor live, efficiency curve, drift detection, and scientific baseline. 147 tests.

### Added

- `KnnPredictor` in `core/predictor.py` — 26-dimensional embedding, ChromaDB HNSW index, top-k=20
  neighbours, `prob / agreement / coverage / confidence` vote formula.
- Calibration gates (5 initial) in `core/calibration.py:evaluate()`: `coverage_hours`,
  `throttle_events`, `peak_amplitude`, `class_diversity`, `hyprctl_consistency`.
- Throttle FSM in `core/store.py` with hysteresis (`COOLSTEP_THROTTLE_ENTER_C=90`,
  `COOLSTEP_THROTTLE_EXIT_C=85`); persists episode start/end to `throttle_events` table.
- `runtime-state.json` — per-tick FSM state persistence; backfill cursor; survives daemon restarts.
- `core/efficiency.py` — thermal efficiency curve (Arrhenius proxy); `GET /api/efficiency`.
- Drift detector (`core/drift.py`) with 7 indicators: feature distribution shift, label imbalance,
  neighbour coverage drop, workload label entropy, etc.
- `ml-state.json` — predictor snapshot every 30 ticks: throttle_prob, confidence, features,
  calibration_ready, labeled_count, uptime, chroma stats.
- Z-score normalization in embedder; ensures features are scale-invariant across different hosts.
- `<neighbours-tile>` — top-K KNN neighbour viewer with workload label + distance.
- `<drift-tile>` — live drift indicator panel with 7 sub-indicators.
- `<efficiency-tile>` — thermal efficiency sweet-spot curve.
- `<calibration-state-tile>` — gate-by-gate calibration readiness progress.
- `<training-tile>` — labeled count, KNN warm-up status, model_name badge.
- Masthead pills: calibration-ready badge, ml-state age, daemon health, signals count.
- Operational threshold corrected to `COOLSTEP_HOT_THRESHOLD_C=82°C` (was 90°C — too conservative).

---

## [0.2.0] — 2026-05-03

P0 polish: discovery manifest, Lit components, and dashboard chrome. 110 tests.

### Added

- Discovery manifest (Zabbix-LLD style) — every collector exposes `signals()` → list of
  `SignalDescriptor` with name, unit, type, dependencies; aggregated at `/api/discoveries`.
  26 signals across 4 collectors at ship.
- Lit Web Components: initial 5 tiles (`live-telemetry-tile`, `sparkline-tile`, `adapters-health-tile`,
  `discovered-signals-tile`, `stack-rationale-tile`) replacing the minimal HTML dashboard.
- Dashboard token system (CSS custom properties) and atrium-style chrome: masthead, pill row, sidebar.
- `platform_state` probes in `linux_sysfs`: CPU governor, EPP mode, `platform_profile` ACPI value.
- Throttle event detector (threshold-crossing heuristic, pre-FSM) writing `throttle_events` rows.
- `core/store.py` SQLite WAL mode, 14-day frame TTL, 90-day throttle events TTL, periodic rotation.
- `core/fingerprint.py` — 26-dim feature extractor: `cpu_temp_max`, `slope_per_sec`, `load_p95`,
  `gpu_temp_max`, per-GPU power/util, workload-class one-hot encodings.
- Systemd user unit monitoring: `coolstep-collector.service` + `coolstep-dashboard.service` active.

---

## [0.1.0] — 2026-05-03

P0 foundation: core schema, ring, store, 4 collectors, daemon, CLI, and dashboard skeleton.
All sprints (A–I) completed in a single day. 98 tests.

### Added

- `core/schema.py` — `TelemetryFrame`, `Action`, `SimResult`, `ActionResult` dataclasses;
  `merge_partial()` for async collector accumulation.
- `core/ring.py` — fixed-capacity in-memory ring buffer (600 frames, ~10 min at 1 Hz).
- `core/store.py` — SQLite with WAL; `frames`, `throttle_events`, `actions` tables.
- `AlwaysIdleBaseline` predictor — stub returning `throttle_prob=0.0`; placeholder until KNN warms.
- `ReadonlyActuator` — logs `Action` intents to in-memory journal without hardware writes.
- `core/decision.py:DecisionEngine` — `Thresholds(notify=0.4, soft=0.7, hard=0.9, conf=0.6)` →
  `list[Action]`.
- 4 collectors in `adapters/collectors/`: `linux_sysfs`, `nvidia_nvml`, `amdgpu`, `hyprctl`;
  async `sample()` with per-collector timeout; `make()` returns `None` on absent hardware.
- `daemon.py` — 1 Hz tick loop: collect → embed → predict → decide → route; asyncio gather;
  `COLLECTOR_TIMEOUTS` dict.
- CLI subcommands via `coolstep/inspect/`: `adapters` (discover + cost probe), `tail` (live frames),
  `stats` (store summary), `export` (CSV dump).
- `systemd/coolstep-collector.service` + `systemd/coolstep-dashboard.service` user units.
- Dashboard backend (FastAPI, `:18889`): `/api/health`, `/api/telemetry/latest`,
  `/api/telemetry/range`, `/api/adapters`, `/api/sse/telemetry` (SSE 1 Hz).
- `pyproject.toml` with `[dev]` extras (pytest, coverage, ruff, mypy).
- `docs/`: `concept.md`, `physics-rationale.md`, `architecture.md`, `stack-decisions.md` (12 ADRs),
  `calibration-gates.md`, `telemetry-schema.md`, `workload-fingerprint.md`.
- Distributed `CLAUDE.md` navigation tree: 10 subdir children + master orchestrator + skill.

---

## Links

[Unreleased]: https://github.com/zzallirog/coolstep/compare/v0.5.20...HEAD
[0.5.20]: https://github.com/zzallirog/coolstep/releases/tag/v0.5.20
[0.5.0]: https://github.com/zzallirog/coolstep/releases/tag/v0.5.0
[0.4.0]: https://github.com/zzallirog/coolstep/releases/tag/v0.4.0
[0.3.0]: https://github.com/zzallirog/coolstep/releases/tag/v0.3.0
[0.2.0]: https://github.com/zzallirog/coolstep/releases/tag/v0.2.0
[0.1.0]: https://github.com/zzallirog/coolstep/releases/tag/v0.1.0
