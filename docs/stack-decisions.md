# Stack decisions (ADR-style)

> Each major stack choice is recorded as a lightweight ADR: what we
> picked, why we picked it, what we considered, and when to revisit.
> The dashboard parses this file and renders it as the "Stack rationale"
> tile, so the headers follow a strict pattern: `## ADR-NNN: Title`.

---

## ADR-001: Python 3.12 for the core

**Decision:** the core is Python 3.12+.

**Alternatives considered:** Rust, Go, C++.

**Why Python:** iteration speed matters in a project with this much ML
and adapter experimentation. The ML ecosystem is here — scikit-learn,
xgboost, hdbscan, sentence-transformers. The maintainer's existing
infrastructure is Python, so the toolchain (ruff, mypy, pytest) is
shared. The overhead is acceptable: telemetry runs at 1 Hz, inference is
infrequent, and the heavy paths (perf subprocess, bpftrace subprocess)
already run outside Python.

**When to revisit:** if the daemon's tick budget breaks under load on a
small ARM host, or if we decide to ship a coolstep-mini for embedded.

---

## ADR-002: `psutil` as baseline cross-platform telemetry

**Decision:** `psutil` is used wherever it suffices; native sysfs paths
override it where they're cheaper or richer.

**Why:** `psutil` works on Linux, Windows and macOS. It removes a large
amount of platform-specific code from the collectors. For the rare cases
where it's slow (per-core load via `/proc/stat` delta) we go native.

**When to revisit:** Windows / macOS adapters in P5 / P6 will lean
harder on `psutil`; the Linux side has already moved past it for
performance.

---

## ADR-003: pynvml for NVIDIA, sysfs for AMD, sysfs for Intel iGPU

**Decision:** NVIDIA via `pynvml`, AMD GPUs via `/sys/class/drm`, Intel
iGPU via `/sys/class/drm/.../gt/`.

**Why:** each vendor has a "blessed" path. NVML is the official NVIDIA
SDK and exposes everything we want. AMD's `amdgpu` driver publishes
everything via sysfs. Intel's `i915` and `xe` drivers expose `gt0`
under sysfs too. Going through `nvidia-smi` subprocess for NVIDIA was
rejected (fork+exec cost, fragile parsing). Going through `radeontop`
for AMD was rejected (CLI tool, not a library).

**When to revisit:** if NVIDIA breaks `pynvml`'s ABI again, we'll have
to ship a vendored copy.

---

## ADR-004: SQLite per host, no time-series database

**Decision:** SQLite WAL with three tables (`frames`,
`throttle_events`, `actions`). No InfluxDB, no Prometheus,
no VictoriaMetrics.

**Why:** the data fits. At 1 Hz with 14-day retention, a host stores
about 1.2 million frames — under 200 MB compressed. SQLite handles
that comfortably. Running a separate TSDB daemon for one host's data
would be heavier than the daemon itself. The dashboard reads SQLite
directly via a connection pool.

**When to revisit:** if multi-host aggregation becomes a real product
feature (rather than separate dashboards per host), we'd ship metrics
to a central Prometheus.

---

## ADR-005: scikit-learn + xgboost for P1 baseline, PyTorch optional later

**Decision:** the predictor pipeline uses scikit-learn for preprocessing
and xgboost for the classifier, both behind a swappable interface.

**Why:** small models, tabular features, fast inference. PyTorch on a 1
Hz tick is overkill and adds 200 MB of dependencies. The classifier
interface (`Predictor` protocol in `core.predictor`) accepts any
callable, so a future neural net or transformer-based fingerprinter can
drop in without changing the rest of the pipeline.

**Note:** P1 is currently KNN-over-ChromaDB; see ADR-013. xgboost is
the planned P2 upgrade.

---

## ADR-006: HDBSCAN for unsupervised workload clustering

**Decision:** workload-class discovery is HDBSCAN over the embedding
space, not k-means.

**Why:** HDBSCAN doesn't require a `k`. The number of workload classes
on any given host is unknown ahead of time — a CI server has 2, a
developer laptop has 8, a gaming machine has 4 with overlapping
characteristics. HDBSCAN handles density-varying clusters and labels
noise points explicitly.

**When to revisit:** if the cluster count stays stable at 4-5 across
all observed hosts, we could replace HDBSCAN with a cheaper fixed-k
approach.

---

## ADR-007: FastAPI + SSE + Lit Web Components for the dashboard

**Decision:** dashboard backend is FastAPI on `:18889`, frontend is
Lit Web Components served via `esm.sh` (no bundler), live updates over
Server-Sent Events.

**Why:** zero build step. The dashboard is editable directly in
`static/components/*.js` and the browser picks up changes on refresh.
Lit gives proper components without React's weight. FastAPI fits the
team's existing Python skill, and its SSE support is built in.

**When to revisit:** if dashboards-as-an-aggregation-product become a
real direction (multi-host, RBAC, themes), the no-build approach won't
scale.

---

## ADR-008: SSE, not WebSocket — **DEPRECATED 2026-05-14**

**Status:** deprecated. SSE removed entirely; tiles poll their own endpoints.

**Original decision:** live updates use Server-Sent Events.

**Why it was chosen:** the dashboard is read-only. We never send
messages from browser to server outside REST calls. SSE handles the one
direction we need with auto-reconnect built into `EventSource`, no
upgrade handshake, no WS frame plumbing.

**Why it was deprecated (balance-plan step IV):** the implementation
turned out to be `while True; row=_latest_row(); yield; sleep(1.0)` —
not push semantics, just polling held open as a long-lived connection.
Four tiles each held an open SSE stream to the same dashboard process.
Long-lived starlette streaming responses contributed to the dashboard
heap growth (anon ≈ 1.6G observed at 4 minutes uptime). Replacing them
with plain `setInterval(1000)` + `fetch('/api/telemetry/latest')` gives
the same UX with simpler lifecycle, finite connections, and less heap
held by streaming buffers. Push semantics, when ever actually needed,
will get reintroduced as a single broadcast channel — not 4 parallel
ones-per-tile.

---

## ADR-009: Adapter discovery via `make() -> Adapter | None`

**Decision:** every adapter module exports a single `make()` function
that returns an instance if the host supports this adapter, or `None`
otherwise.

**Why:** one binary supports laptop, NUC and rack. The registry walks
sibling modules, calls `make()` on each, and silently drops the ones
that return `None`. This is how `coolstep compat` shows which
collectors and actuators activated on this host — it's the same
function the daemon calls at startup.

**When to revisit:** never, hopefully. This is the keystone of the
modular architecture.

---

## ADR-010: Wrap existing tools instead of writing to hwmon / MSR

**Decision:** actuators wrap `asusctl`, `ryzenadj`, `nvidia-smi`,
`cpupower`, `tlp`. They do not write to `/sys/class/hwmon/*/pwm*` or
to MSRs directly.

**Why:** every kernel and every laptop firmware has quirks we don't
have time to learn. `asusctl` knows about ROG-specific quirks.
`ryzenadj` knows about Zen 3 vs Zen 4 PPT semantics. Reusing them
means we inherit their patches when they ship a fix. It also means we
can't cause damage they couldn't — if the vendor tool refuses to set a
value, we just don't set it.

**When to revisit:** for vendors where no community tool exists yet,
we'd consider direct ec_sys access, but only with very loud opt-in.

---

## ADR-011: 1 Hz default, sub-second only on request

**Decision:** the tick rate is 1 Hz. Faster sampling is available via
`COOLSTEP_TICK_HZ=2` (or higher) but isn't recommended.

**Why:** 1 Hz is enough for predicting thermal events (which take
seconds to develop) and easy on the kernel. Per-tick collector budget
is 300 ms total, distributed across whichever adapters are active.
Going to 10 Hz would multiply CPU overhead 10× for no measurable
improvement in prediction quality.

**When to revisit:** if we add a use case that needs sub-second
reaction (gaming frame-pacing? hard-real-time fan control?), we'd
ship a separate fast-loop daemon, not crank the main loop.

---

## ADR-012: ruff + mypy strict for core, relaxed for adapters

**Decision:** `mypy --strict` runs on `coolstep/core/`. Adapters get
ruff but no mypy strict.

**Why:** adapters depend on SDKs with poor type stubs (pynvml has
none, dasbus is partial). Forcing strict typing there means a wall of
`# type: ignore` comments that hide real bugs. The core, where the
types are ours, gets strict treatment. The adapters get tested
heavily through `tests/adapters/`.

**When to revisit:** if Python typing improves enough that `pynvml`
gets official stubs, we'd move adapters to strict.

---

## ADR-013: KNN over ChromaDB for the P1 predictor

**Decision:** the live predictor in v0.5.0 is a KNN with cosine
similarity over a ChromaDB HNSW index. xgboost (per ADR-005) is the
planned upgrade.

**Why:** KNN explains itself. The dashboard's `<neighbours-tile>` shows
the top-5 closest historical states and how each one voted on
`was_hot_in_30s`. A user can trust a prediction by looking at the
neighbours: "these five past states looked like now, four of them got
hot 30 seconds later." That transparency is worth more than the small
accuracy gain xgboost would bring at this stage.

**When to revisit:** when we have enough labelled data per host that
xgboost actually outperforms KNN by enough to justify the loss of
explainability.

---

## ADR-014: Efficiency proxy = `work_per_degree`, not real package power

**Decision:** the efficiency curve uses
`(load × freq_mhz / 1000) × 100 / (T_chip − T_ambient)` instead of
`work / package_power_w`.

**Why:** package power is unreadable on most AMD desktop hosts and
root-only on Intel post-CVE-2020-8694. The proxy yields the same
sweet-spot and knee locations on hosts where both are available. See
[`efficiency-curve.md`](efficiency-curve.md) for the comparison.

**When to revisit:** when `rapl_energy` becomes universally readable
(probably never on consumer AMD without `amd_energy` mainline support),
or when we ship privileged-mode coolstep for hosts that grant it.

---

## ADR-015: Game-mode optimization actuator as a P2.5 stub

**Decision:** when `game-mode.service` is active, coolstep's
fan-curve-bias actuator returns `supports(verb) == False` by default.
A separate `game_mode_optimizer` actuator can be enabled to
coordinate.

**Why:** game-mode already optimizes thermal/performance for the
running game. Stacking coolstep on top would fight it. The default is
to defer. The optimizer actuator, off by default, is for users who
specifically want cooperation rather than handoff.

**When to revisit:** when game-mode exposes an API for coordinated
biasing instead of "all or nothing."

---

## ADR-020: Bayesian shrinkage on ResidualBank correction

**Decision:** `ResidualBank.correct()` returns a shrunk posterior view
of the per-bucket residual statistics, not the raw EWMA mean / Welford
σ.  Each bucket is treated as starting with k=5 pseudo-observations at
zero mean and σ₀=log1p(2.0°C) in log-residual space.

  μ_post   = (n·μ_data + k·0)               / (n + k)
  ν_post²  = (m2 + k·σ₀² + n·k/(n+k)·μ²)    / (n + k − 1)

The dashboard / MetaPredictor reads only the shrunk view; the bank's
internal `RunningStat` keeps storing raw observations as before
(shrinkage is a read-time projection, not a write transform).

**Why:** an operator-flagged pathology, 2026-05-12.  A fresh bucket
with n=2 agreeing samples of −6°C reported correction = −6.23°C with
σ = 0.01°C.  That's a strong claim from two observations — Welford's
incremental variance is zero whenever consecutive samples agree to
within noise, regardless of how confident the underlying generator
truly is.  The MetaPredictor then composed a `confidence × certainty`
of ≈ 0.95 on a bucket the daemon had seen for ~10 seconds, and the
cockpit drew a `+5s` forecast with a σ-band visually narrower than
the sensor's own ±0.5°C jitter.

Shrinkage damps both knobs at small n:
  n=2,  k=5  → correction is 28% of pure data, σ ≥ ~1.7°C linear
  n=10, k=5  → 67% of data, σ wider than pure σ_data
  n=200,k=5  → 97% of data, σ tracks pure σ_data
The disagreement term `(n·k/n_eff)·μ²` widens σ further whenever the
data and prior disagree — exactly the "honest uncertainty" face the
operator wants when a young bucket sees a strong signal.

The composed bucket_certainty gate in `predictor_meta.py` (drops to
0.4 when n<5) was a coarser version of the same idea — shrinkage is
its smooth, continuous form, and it acts on the *correction value*
not just the confidence score.

**Why these constants:**
  k=5     — first ~3 obs in a new bucket damp the correction by ≥60%
            (cockpit-readable "we're still learning"); convergence to
            near-pure data view by ~15 observations matches a single
            5-second prediction horizon's worth of sampling
  σ₀=2°C  — residual scale a calibrated predictor exhibits in steady
            regime on this target chip (Ryzen 9 7940HS, observed in
            residual-log tail medians 2026-05-08…12)

**When to revisit:** if a future predictor stabilises with median
residual <1°C in calibrated buckets, shrinking σ₀ proportionally
(=log1p(1.0)) preserves the same "first 3 obs are preliminary" feel
without overstating the prior's uncertainty.  Also: if the workload
fingerprint earns its own ADR (P2.7 spike archive backfill), shrinkage
constants may want per-fingerprint values rather than one global pair.

**Cross-refs:** `coolstep/core/residual_meta.py:correct()`, tests in
`tests/test_residual_meta.py::test_shrinkage_*`, the operator screenshot
from 2026-05-12 captured in `[[project-coolstep-p2_4-adaptive-and-incidents]]`.

---

## ADR-021: Forecast curve anchors on meta-corrected endpoint

**Decision:** the cockpit's dashed forecast line is rescaled so that
T(horizon) lands exactly on `cur.predicted` — the meta-corrected
value the predictor actually reports — instead of T0 + s·τ·(1−e^(−h/τ))
from the raw slope.

  T(t) = T0 + (cur.predicted − T0) · (1 − e^(−t/τ)) / (1 − e^(−h/τ))

The saturation *shape* (Newton-cooling factor) is preserved; only the
endpoint is anchored.  When the meta correction is zero this formula
collapses to the original raw saturation curve, so calibrated-bucket
behaviour is unchanged.

**Why:** operator-flagged 2026-05-12.  In a `past knee` event with
slope = +2.10°C/s and a fresh bucket (n=2, meta-correction = −6.23°C),
the raw saturation line drew up toward 91°C while the predicted
endpoint ring sat at 85°C.  The σ-band envelops both but the visual
mismatch reads as a bug — "the line says 91, the ring says 85, which
do I trust?"  Now the line *is* what the model predicts, with
matching σ-band semantics; the ring at the line's tip is decorative,
not a separate datum to reconcile.

The trade-off is the loss of "see physics vs. learned" as a visual.
ADR-020 + this ADR together make the meta layer first-class — the
dashboard shows the model's prediction, not raw physics underneath
it.  If we want raw physics back as context, it should be a separate
faint ghost line, not a louder primary line.

**Cross-refs:** ADR-018 (cockpit tile),
`coolstep/dashboard/static/components/predictor-cockpit-tile.js:_draw`.

---

## ADR-022: HNSW backend swap-in via runtime selector (preserve chromadb fallback)

**Decision:** the predictor's KNN store becomes pluggable behind
`COOLSTEP_KNN_BACKEND={chroma,hnsw}` (`coolstep/core/knn.py:make_knn_store`).
Default stays `chroma` for backward compatibility.  `hnsw` mode constructs
`coolstep/adapters/storage/hnsw.py:HnswStore` — a chroma-shaped wrapper
around `chroma-hnswlib` (the vendored fork chromadb already pulls).  Same
public surface (`discover/available/add/query/query_stable/update_metadata/
count/count_labeled/dir_size_bytes/list_stable/list_unlabeled`), different
backing index.

**Why:** chromadb's `PersistentClient.query` under a constrained CPU
budget (10% slice quota) takes ~9.5s at 42k vectors.  At a 10Hz daemon
tick that means ~95 ticks reuse a stale prediction during each refresh
— operator-visible as a frozen residual trail.  hnswlib query stays
under 1ms in the same conditions.

Synthetic 42k-vector benchmark, unconstrained:

| Backend    | query p50 | query p95 | upsert/s |
|------------|-----------|-----------|----------|
| chroma     | 329 ms    | 342 ms    | n/a      |
| hnsw       | 0.3 ms    | 0.4 ms    | 5026     |

The 1000× factor isn't the chroma library being broken — it's the
SQLite + HNSW double-bookkeeping cost.  Both backends keep working;
hnsw gives more headroom under tight slice budgets.

**Dep pin: `chroma-hnswlib>=0.7.6`, NOT upstream `hnswlib`.**  Both
packages install a `hnswlib` module file with the same name; `pip install
hnswlib` silently overwrites chromadb's vendored fork, breaking
`load_index(..., is_persistent_index=True)` in chromadb 0.6.3.  The fork
is a strict superset of the API HnswStore uses.

**Migration:** `scripts/reindex_hnsw_from_store.py` rebuilds the HNSW
index from `data/store.db` by re-embedding through the same Embedder
that produced the chroma vectors — bit-identical output.  Used instead
of a chromadb→hnsw direct copy because the in-place chromadb HNSW
file becomes unreadable when upstream `hnswlib>=0.8` writes mix with the
fork's format.

**Cross-refs:** `coolstep/adapters/storage/hnsw.py`,
`coolstep/core/knn.py`, `coolstep/core/storage_common.py`,
`scripts/{reindex_hnsw_from_store,migrate_chroma_to_hnsw,cutover_to_hnsw,hnsw_rollback,watch_hnsw_health}.{py,sh}`.

---

## ADR-023: Three trust regimes surfaced from Bayesian shrinkage state

**Decision:** the implicit prior / shrunk / confident regimes already
present in `ResidualBank.correct()` (Bayesian shrinkage by `n` vs
`PRIOR_K=5`) become a named `TrustMode` enum and a `correct_with_trust()`
helper.  The daemon surfaces the current bucket's mode + sample count
in `ml-state.json`; the dashboard cockpit renders `○ prior / ◐ shrunk /
● confident` glyphs next to the refresh-health strip.

  - `prior`     (n == 0)               — no data, zero correction, prior σ
  - `shrunk`    (1 ≤ n < PRIOR_K)      — damped toward 0, σ wide
  - `confident` (n ≥ PRIOR_K)          — pure EWMA correction

**Why:** these regimes are load-bearing for prediction trustworthiness
but were entirely implicit.  Operators reading the cockpit couldn't tell
a fresh-bucket "+0°C" correction (mode=prior, "we don't know") from a
fitted-bucket "+0°C" correction (mode=confident, "we've seen this and
it cancels out") — visually identical, semantically opposite.

**Cross-refs:** `coolstep/core/residual_meta.py:TrustMode`,
`coolstep/dashboard/static/components/predictor-cockpit-tile.js:_renderRefreshHealth`.

---

## ADR-024: Drift-triggered Embedder refit with parity gate

**Decision:** when `core/cluster_drift.py:detect_cluster_drift` flags
positive drift on ≥ 3 consecutive 6-hourly checks (each gap ≥ 1h), the
daemon background-fires `core/embedder_refit.py:refit_and_swap` in a
worker thread.  Refit re-fits the Embedder on the last 10 000 frames,
validates ≥ 0.9 top-K parity on a 5% holdout against the live index,
and only swaps via atomic rename (`data/hnsw.staging/` →
`data/hnsw/`) when parity passes.  A SIGKILL during the rename window
leaves `data/hnsw.backup/` for manual recovery; `HnswStore.discover`
restores it on next start if `data/hnsw/` is missing.

**Why:** the Embedder is fit-once at first start and frozen thereafter.
Hardware swap, BIOS update, new sustained workload class all shift the
embedding space → KNN matches become stale → prediction accuracy
degrades silently.  The drift detector already existed but only logged
a signal; nothing acted on it.  This wires the action with a
safety-first gate (parity reject keeps the old index; all-negative
"improving" drift maps do not count toward the streak).

**Cross-refs:** `coolstep/core/cluster_drift.py:DriftGate`,
`coolstep/core/embedder_refit.py`, `coolstep/daemon.py:_embedder_refit_check`.

---
