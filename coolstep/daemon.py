"""coolstep collector daemon.

Per-tick lifecycle (see docs/architecture.md):

  1. sample all collectors (async, per-collector timeout)
  2. merge → TelemetryFrame
  3. fingerprint.extract → features
  4. predictor.predict → Prediction
  5. decision.decide → list[Action]
  6. route actions to actuator (P0: readonly only)
  7. ring.push + store.write_frame
  8. write ml-state.json snapshot every N ticks

Designed to run as user-systemd unit. Catches SIGTERM, flushes store, exits 0.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import click

from coolstep.adapters.actuators import discover as discover_actuators
from coolstep.adapters.actuators._base import Actuator
from coolstep.adapters.collectors import discover as discover_collectors
from coolstep.core import drift as drift_mod
from coolstep.core import fingerprint as fp
from coolstep.core.backfill import backfill_labels
from coolstep.core.backfill import backfill_labels as _backfill_from_events
from coolstep.core.calibration import evaluate as eval_calibration
from coolstep.core.cluster_drift import DriftGate, detect_cluster_drift
from coolstep.core.decision import DecisionEngine
from coolstep.core.embedder_refit import refit_and_swap
from coolstep.core.embedding import Embedder
from coolstep.core.knn import make_knn_store
from coolstep.core.predictor import KnnPredictor, TrajectoryBaseline
from coolstep.core.predictor_meta import MetaPredictor
from coolstep.core.residual_meta import ResidualBank, classify_trust
from coolstep.core.ring import Ring
from coolstep.core.schema import (
    LABEL_COOL,
    LABEL_HOT,
    LABEL_UNKNOWN,
    Action,
    ActionResult,
    ActionVerb,
    TelemetryFrame,
    merge_partial,
)
from coolstep.core.store import Store
from coolstep.core.tuned_profile_watch import ProfileWatcher

LOOKAHEAD_SEC = 30.0
# Operational threshold (above the per-host efficiency knee, ~5-10°C below
# thermald hard-throttle). 82°C is conservative for AMD 7940HS post-knee zone.
# Override via env COOLSTEP_HOT_THRESHOLD_C if your knee is lower/higher.
HOT_THRESHOLD_C = float(os.environ.get("COOLSTEP_HOT_THRESHOLD_C", 82.0))

DEFAULT_PERIOD = 0.1  # P2.9.6: 10Hz (~100ms tick budget, btop-class)
DEFAULT_COLLECTOR_TIMEOUT = 0.3
# Per-collector overrides (collector.name → timeout_s). hyprctl делает
# fork+exec+IPC+JSON parse ~95 ms на спокойной системе, и легко >300 ms когда
# RAM/CPU pressure высокое. 0.3s deadlock'ил cache warm-up (incident
# 2026-05-10 hyprctl_consistency=100% bad).
COLLECTOR_TIMEOUTS: dict[str, float] = {"hyprctl": 2.0}

# Throttle FSM thresholds — hysteresis: вход в «hot» при cpu_temp >= ENTER,
# выход при <= EXIT. AMD 7940HS thermal trip ~95°C, knee ~85°C. 90/85
# консервативно: пишем event'ом продолжительные эпизоды близкие к throttle,
# не одиночные spikes. min duration = MIN_DURATION_S (skip микро-эпизоды).
THROTTLE_ENTER_C = float(os.environ.get("COOLSTEP_THROTTLE_ENTER_C", 90.0))
THROTTLE_EXIT_C = float(os.environ.get("COOLSTEP_THROTTLE_EXIT_C", 85.0))
THROTTLE_MIN_DURATION_S = float(os.environ.get("COOLSTEP_THROTTLE_MIN_S", 3.0))
# P2.1 hardware-safety belt: any actuator that wrote to hardware must auto-
# revert within TTL_GRACE_SEC of `action.expires_at`, even if no new
# prediction fires. REARM_GAP_SEC prevents thrashing when a fresh action
# would apply ≪15 s before the previous one expires (we let the current
# bias ride out instead of stacking writes).
TTL_GRACE_SEC = float(os.environ.get("COOLSTEP_TTL_GRACE_SEC", 5.0))
REARM_GAP_SEC = float(os.environ.get("COOLSTEP_REARM_GAP_SEC", 15.0))
# Public aliases — tests + external callers reference these env-prefixed names.
# Single source of truth: env var → TTL_GRACE_SEC / REARM_GAP_SEC; the
# `COOLSTEP_`-prefixed names just re-export so `from coolstep.daemon import
# COOLSTEP_TTL_GRACE_SEC` works.
COOLSTEP_TTL_GRACE_SEC = TTL_GRACE_SEC
COOLSTEP_REARM_GAP_SEC = REARM_GAP_SEC
# Actuators whose `apply()` doesn't change hardware — no revert needed.
# readonly_log just appends to its in-memory journal.
_NO_REVERT_ACTUATORS: frozenset[str] = frozenset({"readonly_log"})
ML_STATE_FILE = "ml-state.json"
RUNTIME_STATE_FILE = "runtime-state.json"

# P2.3 — operational modes. Default is the original anticipatory-cooling
# behaviour. `quiet` flips the policy to subtractive fan-curve bias under
# confidently-calm predictions. `off` blocks every actionable verb (notify
# still flows). The mode is persisted in `runtime-state.json:mode` so the
# dashboard and the daemon agree across restarts without an IPC channel —
# the daemon re-reads the file each tick before deciding (cheap, ~200B).
VALID_MODES: frozenset[str] = frozenset({"cool", "quiet", "off"})
DEFAULT_MODE = "cool"

log = logging.getLogger("coolstep.daemon")


def _coolstep_home() -> Path:
    return Path(os.environ.get("COOLSTEP_HOME", str(Path.home() / "coolstep" / "data")))


def _log_backfill_exception(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.debug("incremental backfill failed: %r", exc)


def _log_spike_incident_exception(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.debug("spike incident write failed: %r", exc)


def _log_rotate_exception(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("store rotate failed: %r", exc)


def _log_chroma_guard_exception(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.debug("chroma size guard failed: %r", exc)


class Daemon:
    def __init__(
        self,
        period_sec: float = DEFAULT_PERIOD,
        ring_capacity: int = 600,
        store_path: Path | None = None,
        ml_state_path: Path | None = None,
    ) -> None:
        home = _coolstep_home()
        home.mkdir(parents=True, exist_ok=True)
        self.period = period_sec
        self.ring = Ring(capacity=ring_capacity)
        self.store = Store(store_path or home / "store.db")
        self.ml_state_path = ml_state_path or home / ML_STATE_FILE
        self.collectors = discover_collectors()
        self.actuators = discover_actuators()
        # P2.4 — repair `enabled: false` regression on supported actuators.
        # asusctl_fan_curve can drift to `enabled: false` per-fan (e.g.
        # after `asusctl … --default`) and silently leave firmware fan
        # logic in charge. Probe + flip at startup so the user's curve
        # actually takes effect from the first tick.
        for actuator in self.actuators:
            ensure = getattr(actuator, "ensure_enabled", None)
            if callable(ensure):
                try:
                    ensure()
                except Exception as exc:  # noqa: BLE001
                    log.warning("ensure_enabled failed for %s: %r", actuator.name, exc)
        self.embedder = Embedder()
        # Persisted embedder stats survive restarts so newly-embedded
        # frames stay comparable with vectors already in chroma. Without
        # this, daemon would refit on the post-restart ring window and
        # the normalization (median/MAD) skews vs archive → KNN broken.
        self._embedder_stats_path = home / "embedder-stats.json"
        self._embedder_stats_locked = self.embedder.load_stats(self._embedder_stats_path)
        if self._embedder_stats_locked:
            log.info("embedder: loaded persisted stats from %s — refits frozen",
                     self._embedder_stats_path.name)
        self.chroma = make_knn_store()
        self.chroma.discover()
        if self.chroma.available:
            self.predictor = MetaPredictor(
                base=KnnPredictor(self.embedder, self.chroma),
            )
        else:
            # ChromaDB unavailable (cold start, CVE-locked, or
            # COOLSTEP_CHROMA_DISABLED=1).  TrajectoryBaseline +
            # MetaPredictor still gives a learning forecast: Newton
            # saturation as the base + bucketed residual correction.
            # See ADR-017.
            self.predictor = MetaPredictor(base=TrajectoryBaseline())
        self.decision = DecisionEngine()
        self.calibration_ready = False
        self._stop = asyncio.Event()
        self._tick_count = 0
        # P2.9.6 phase 1: chroma writes happen out-of-band so the per-tick
        # loop doesn't pay HNSW index-rebuild cost.  Bounded queue +
        # single drain worker (chroma client isn't thread-safe for
        # concurrent writes).  Worker started lazily in run().
        self._chroma_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=200)
        self._chroma_worker_task: asyncio.Task[None] | None = None
        # P2.9.6 phase 2: predictor.predict triggers a KNN query on
        # 42k+ chroma vectors (~500ms idle, ~5s under concurrent write).
        # Block-on-tick → daemon at 0.06Hz.  Solution: cache predict
        # result; refresh async at most every predict_refresh_sec.
        # Tick reads cached + decision proceeds with last known prediction.
        # First tick blocks once to seed the cache.
        self._cached_prediction: Any = None
        self._cached_prediction_ts: float = 0.0
        self._predict_refresh_sec: float = 3.0
        self._predict_task_inflight: bool = False
        # Operator-visibility counters (2026-05-13): refresh ticks that re-used
        # the stale cached_prediction because the previous refresh was still
        # running.  When this counter climbs faster than once per second, the
        # KNN backend is bottlenecking the predictor — dashboard surfaces it
        # as "skipped" near the residual trail.
        self._predict_refresh_skipped: int = 0
        self._predict_refresh_started_at: float = 0.0
        self._predict_refresh_last_ms: float = 0.0
        # P2.9.6 phase 3: chroma.count() and chroma.dir_size_bytes()
        # were called from _dump_ml_state EVERY tick — they walk the
        # HNSW index and the persist dir respectively, ~5-15s on a
        # 42k-vector store.  Cache them, refresh every 30 ticks.
        self._cached_chroma_count: int = 0
        self._cached_chroma_dir_bytes: int = 0
        self._cached_labeled_count: int = 0
        self._chroma_stats_refresh_every: int = 30
        self._labelled_window: list[TelemetryFrame] = []  # buffer for backfill
        self._started_at = time.time()
        # Throttle FSM: idle ↔ hot, hysteresis 90/85°C. Open «hot» episode
        # буфер'ит start ts + peak; закрытие — `write_throttle_event`. Без
        # этого calibration gate `throttle_events` хронически 0 (incident
        # 2026-05-10).
        self._throttle_state: str = "idle"
        self._throttle_start_ts: float = 0.0
        self._throttle_peak_temp: float = 0.0
        self._throttle_workload_at_start: str | None = None
        # Persistent runtime state (throttle FSM + backfill cursor) — survives
        # daemon restart so open «hot» episodes don't disappear and chroma
        # vectors don't silently leak as LABEL_UNKNOWN forever (calibration-
        # audit risk #4 + predictor-audit risk #6).
        self._runtime_state_path = home / RUNTIME_STATE_FILE
        self._backfill_cursor_ts: float = 0.0
        # Operational mode. Re-read from runtime-state.json each tick so
        # the dashboard's POST /api/mode takes effect without a daemon
        # restart. Cached value on the instance for log lines.
        self._mode: str = DEFAULT_MODE
        # P2.4 — cached snapshot updated at end of each tick. The incident
        # logger (quiet_safety_eject / throttle_fsm close) reads this so
        # records carry the predictor's view as of *the moment before*
        # the incident, not the moment after.
        self._last_incident_snapshot: dict[str, Any] = {}
        # P2.5 Heavy-3 — session segmentation + efficiency calibration.
        # EventSegmenter is fed per tick; its boundaries get tagged on
        # the chroma write so future KNN queries can filter by session.
        # The efficiency analyser runs every N ticks (alongside the
        # existing drift-history append).
        from coolstep.core.event_segmentation import EventSegmenter
        self._segmenter = EventSegmenter()
        self._last_session_id: str | None = None
        self._efficiency_analyse_every_ticks = int(
            os.environ.get("COOLSTEP_EFFICIENCY_TICKS", "600")
        )
        self._last_efficiency_at_tick: int = 0
        # P2.1: armed (currently-applied) hardware actions, keyed by
        # actuator.name → (action, applied_at_unix). Auto-reverted on TTL
        # expiry, shutdown, or at startup if previous run crashed with
        # something armed. readonly_log is excluded (see _NO_REVERT_ACTUATORS).
        self._armed_actions: dict[str, tuple[Action, float]] = {}
        self._backfill_interval_ticks = int(
            os.environ.get("COOLSTEP_BACKFILL_INTERVAL_TICKS", "600")
        )
        self._backfill_last_tick: int = 0  # absolute tick at which last backfill ran
        # Inflight guard for the incremental backfill batch. Without it,
        # a slow batch (N rows × 5-30 ms each = 100-500 ms) blocked the
        # tick coroutine — `await` returns to the event loop but the
        # *next* tick can't start until this one finishes, so the
        # ml-state.json freshness gap matched the batch duration. Wrap
        # in `asyncio.to_thread` + Task handle: the tick returns
        # immediately and the next batch isn't scheduled until the
        # previous one drains, so we never pile up.
        self._backfill_inflight_task: asyncio.Task[Any] | None = None
        # 30s `_backfill_labels` inflight guard. Same shape as the batch
        # backfill task above — labels deep buffer entries (≥30s old)
        # against the live ring; on a busy buffer the chroma metadata
        # updates take 1-6 s, which used to stall the tick coroutine for
        # that entire duration via `await asyncio.to_thread`.
        self._backfill_labels_inflight_task: asyncio.Task[Any] | None = None
        # Pool of fire-and-forget background tasks (spike-incident writes,
        # rotate, chroma size guard). Tracked so shutdown can await them
        # — otherwise asyncio prints "Task was destroyed but it is pending"
        # at process exit. Each task removes itself via a done-callback.
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        # P3.0 Residual log + pending-prediction FIFO.  Each prediction is
        # held in `_pending_predictions` until its horizon elapses; at that
        # moment we know the actual outcome and can append a ResidualRecord.
        # The FIFO is unbounded but self-trimming — entries leave it within
        # horizon_sec, so it never grows beyond `horizon_sec / period`.
        from coolstep.core.residual_log import ResidualLog
        self.residual_log = ResidualLog(home / "residual-state.jsonl")
        # Spike detector: closes the loop between high residuals ("the
        # predictor was surprised") and the incident archive ("save this
        # so the next workload of this shape gets recognised").  Pure
        # state machine — feed it every validated residual, get a
        # SpikeRecord on closure to log as Incident + (future) backfill
        # as a labelled training sample once Chroma is alive again.
        from coolstep.core.spike_detector import SpikeDetector
        self.spike_detector = SpikeDetector()
        # Pending list: (predicted_at_ts, predicted_temp_c, model_name,
        #                bucket_key, features_snapshot)
        self._pending_predictions: list[tuple[float, float, str, tuple[int, ...] | None, dict[str, float]]] = []
        # MetaPredictor's learning state — rebuilt from disk at startup.
        # If the wrapped predictor is the meta one, replay its residuals.
        if isinstance(self.predictor, MetaPredictor):
            self.predictor.bank = ResidualBank.from_log(self.residual_log)
            log.info(
                "meta-predictor: replayed %d residual buckets from %s",
                len(self.predictor.bank), self.residual_log.path,
            )
        # Profile watcher: explicit /etc/tuned/active_profile signal.
        # Polled every 30 ticks; on transition we decay the residual
        # bank (preserve half the learning) so the next 5-10 ticks
        # re-converge to the new thermal envelope.
        self._profile_watcher = ProfileWatcher()
        self._tuned_profile_changed_at: float = 0.0
        # P2.9.7: throttle load_jump→bank decay to once per 30s. Daemon startup
        # and rapid load oscillation can fire multiple load_jump boundaries
        # in a few seconds; back-to-back decays compound (0.3^N → ~0) and
        # destroy hard-won bank state. 30s window matches tuned-profile
        # decay cadence (every_30s poll), so the two paths cost similarly.
        self._last_bank_decay_at: float = 0.0
        # P2.8+ drift-triggered embedder refit. Gate fires after N consecutive
        # drift detections ≥ 1h apart; refit runs in a thread to keep ticks
        # responsive. Cooldown via wall-clock so tick-rate changes don't
        # disturb cadence. Env knob: COOLSTEP_REFIT_CHECK_HOURS (default 6).
        self._drift_gate: DriftGate = DriftGate()
        # Clamp to a half-hour floor: a misconfigured 0 / negative env would
        # turn the wall-clock gate "always due", spawning a fresh refit
        # thread on every tick → daemon OOM under any sustained load.
        _refit_hours_raw = float(os.environ.get("COOLSTEP_REFIT_CHECK_HOURS", "6"))
        self._refit_check_hours: float = max(0.5, _refit_hours_raw)
        self._last_refit_check_at: float = 0.0
        # Inflight guard: stops a clock-skew double-fire from racing two
        # `staging_dir.rename(live_dir)` calls (second would FileNotFoundError
        # mid-flight and leave the live HNSW directory half-renamed).
        self._refit_inflight: bool = False
        self._refit_log_path: Path = home / "embedder-refit.log"
        self._restore_runtime_state()
        # Backfill LABEL_UNKNOWN chroma vectors from `throttle_events`. Closes
        # the gap where the live +30s buffer missed an episode (long uptime
        # gap, or window-peak <82°C while FSM tripped at 90°C). Wrapped in
        # try/except — must never block startup.
        try:
            _backfill_from_events(self.store.path, self.chroma)
        except Exception as exc:  # noqa: BLE001
            log.warning("backfill failed: %r", exc)
        self._recover_armed_actions()
        self._recover_orphan_labels()

    def request_stop(self) -> None:
        log.info("stop requested")
        self._stop.set()

    async def run(self, max_ticks: int | None = None) -> None:
        log.info(
            "daemon start: %d collectors, %d actuators, period=%.2fs",
            len(self.collectors),
            len(self.actuators),
            self.period,
        )
        if self.chroma.available and self._chroma_worker_task is None:
            self._chroma_worker_task = asyncio.create_task(
                self._chroma_drain_worker()
            )
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                await self._tick()
                self._tick_count += 1
                if max_ticks is not None and self._tick_count >= max_ticks:
                    break
                elapsed = time.monotonic() - t0
                sleep_for = max(0.0, self.period - elapsed)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                except TimeoutError:
                    pass
        finally:
            self._revert_all_armed("shutdown")
            self._persist_runtime_state()
            if self._chroma_worker_task is not None:
                self._chroma_worker_task.cancel()
                try:
                    await self._chroma_worker_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                self._chroma_worker_task = None
            # Drain fire-and-forget background tasks (spike-incident
            # writes, rotate, chroma size guard, backfill). 2 s ceiling
            # so a stuck task can't hang shutdown indefinitely.
            pending = [t for t in self._bg_tasks if not t.done()]
            if self._backfill_inflight_task is not None and not self._backfill_inflight_task.done():
                pending.append(self._backfill_inflight_task)
            if (
                self._backfill_labels_inflight_task is not None
                and not self._backfill_labels_inflight_task.done()
            ):
                pending.append(self._backfill_labels_inflight_task)
            if pending:
                try:
                    await asyncio.wait(pending, timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass
            self.store.close()
            log.info("daemon stopped after %d ticks", self._tick_count)

    async def _refresh_chroma_stats(self) -> None:
        """Background refresh of cached chroma stats (P2.9.6 phase 3).
        chroma.count() / count_labeled() / dir_size_bytes() each walk the
        HNSW index or persist dir (~5-15s on 42k vectors); kept out of
        the hot tick path."""
        try:
            count, labeled, size = await asyncio.gather(
                asyncio.to_thread(self.chroma.count),
                asyncio.to_thread(self.chroma.count_labeled),
                asyncio.to_thread(self.chroma.dir_size_bytes),
            )
            self._cached_chroma_count = int(count or 0)
            self._cached_labeled_count = int(labeled or 0)
            self._cached_chroma_dir_bytes = int(size or 0)
        except Exception as exc:  # noqa: BLE001
            log.debug("chroma stats refresh failed: %r", exc)

    def _guarded_refit_check(self) -> None:
        """Wrap `_embedder_refit_check` so `_refit_inflight` is always cleared,
        even if the underlying call raises (missing store.db, hnsw load, etc.).
        Without the guard a single failed run would pin the flag and prevent
        any future refit until daemon restart.
        """
        try:
            self._embedder_refit_check()
        except Exception as exc:  # noqa: BLE001
            log.warning("embedder_refit_check raised: %r", exc)
        finally:
            self._refit_inflight = False

    def _embedder_refit_check(self) -> None:
        """Drift-triggered embedder refit check (P2.8+).

        Runs detect_cluster_drift against the live chroma/hnsw store, feeds
        the result into the DriftGate, and — when the gate fires — calls
        refit_and_swap() via asyncio.to_thread in the caller. This method is
        sync; the async wrapper above schedules it off the hot loop.
        """
        if not self.chroma.available:
            return
        drift_map = detect_cluster_drift(self.chroma)
        self._drift_gate.record(drift_map)
        if not self._drift_gate.should_refit():
            log.debug(
                "embedder_refit_check: streak=%d/%d — not yet",
                self._drift_gate.streak_len, self._drift_gate.min_consecutive,
            )
            return
        log.info(
            "embedder_refit_check: gate fired (streak=%d) — scheduling refit",
            self._drift_gate.streak_len,
        )
        spike_active = self.spike_detector.state.active
        report = refit_and_swap(
            store_path=self.store.path,
            hnsw_store=self.chroma,  # type: ignore[arg-type]
            current_embedder=self.embedder,
            embedder_stats_path=self._embedder_stats_path,
            refit_log_path=self._refit_log_path,
            spike_active=bool(spike_active),
        )
        log.info(
            "embedder_refit: accepted=%s parity=%.3f frames=%d duration=%.0fms reason=%r",
            report.accepted, report.parity_pct, report.frames_used,
            report.duration_ms, report.skipped_reason,
        )
        if report.accepted:
            self._drift_gate.reset()

    async def _refresh_prediction(self, features: dict, window: list) -> None:
        """Background predictor refresh (P2.9.6 phase 2).
        Runs predictor.predict in a thread (KNN query is sync + slow);
        updates the cache when done.  In-flight flag prevents stacking
        so a slow query doesn't queue up 30 concurrent invocations."""
        try:
            pred = await asyncio.to_thread(
                self.predictor.predict, features, window,
            )
            self._cached_prediction = pred
            self._cached_prediction_ts = time.time()
            self._predict_refresh_last_ms = (
                self._cached_prediction_ts - self._predict_refresh_started_at
            ) * 1000.0
        except Exception as exc:  # noqa: BLE001
            log.warning("predict refresh failed (%s): %r",
                        type(exc).__name__, exc)
        finally:
            self._predict_task_inflight = False

    async def _chroma_drain_worker(self) -> None:
        """Single-consumer drainer for the chroma write queue (P2.9.6).
        chromadb 0.6.3 PersistentClient.add() rebuilds the HNSW index on
        every call — ~3s for a 42k-vector collection on this host.  If
        the tick loop awaited that directly, daemon ticked at ~0.3Hz.
        Worker pulls frames from the queue and writes them in a thread,
        sequentially (chroma client isn't thread-safe for concurrent
        writes).  Drops are acceptable — fresh frames win over stale."""
        while not self._stop.is_set():
            try:
                frame = await asyncio.wait_for(
                    self._chroma_queue.get(), timeout=1.0,
                )
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            try:
                await asyncio.to_thread(self._chroma_write, frame)
            except Exception as exc:  # noqa: BLE001
                log.warning("chroma drain failed (%s): %r",
                            type(exc).__name__, exc)

    def _revert_all_armed(self, reason: str) -> None:
        """Best-effort revert of every armed actuator (shutdown path).
        Silent — caller is finally-block, must never crash."""
        for name in list(self._armed_actions.keys()):
            actuator = self._find_actuator(name)
            if actuator is not None:
                try:
                    actuator.revert()
                    log.info("actuator_revert: name=%s reason=%s", name, reason)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "actuator_revert failed on %s name=%s exc=%r",
                        reason, name, exc,
                    )
            self._armed_actions.pop(name, None)

    async def _tick(self) -> None:
        # P2.9.6 — per-stage timing dump every 30 ticks until tick rate
        # is back to ~1Hz steady-state.  Removed once issue closed.
        _stage_t = {}
        _t0 = time.monotonic()
        def _mark(name: str) -> None:
            _stage_t[name] = round((time.monotonic() - _t0) * 1000, 1)
        frame = TelemetryFrame(timestamp=time.time())
        partials = await asyncio.gather(
            *[self._sample_with_timeout(c) for c in self.collectors],
            return_exceptions=False,
        )
        _mark("sample")
        for partial in partials:
            if partial:
                merge_partial(frame, partial)

        window = self.ring.window(self.ring.capacity)
        window.append(frame)  # include current
        features = fp.extract(window)
        if frame.workload is not None:
            features.update(frame.workload.rolling_features)

        # P2.1: sweep armed actions BEFORE deciding new ones — keeps the
        # hardware-safety belt independent of the predictor firing again.
        self._sweep_expired_armed(now=time.time())

        # P2.9.6 phase 2: predictor.predict can take 500ms-5s depending
        # on chroma load.  Cache + async-refresh:
        #   - cold start: block once to seed cache (decision needs a value)
        #   - subsequent: read cache, fire background refresh if stale
        # Tick remains fast even on a slow KNN backend.  The few seconds
        # of staleness are acceptable for a 30s-horizon predictor.
        now_ts = time.time()
        stale = (
            self._cached_prediction is None
            or (now_ts - self._cached_prediction_ts) > self._predict_refresh_sec
        )
        if stale and not self._predict_task_inflight:
            if self._cached_prediction is None:
                # Cold: seed cache synchronously so decision has a value.
                prediction = await asyncio.to_thread(
                    self.predictor.predict, features, window
                )
                self._cached_prediction = prediction
                self._cached_prediction_ts = now_ts
            else:
                # Warm: refresh in background, reuse cached prediction.
                self._predict_task_inflight = True
                self._predict_refresh_started_at = now_ts
                asyncio.create_task(self._refresh_prediction(features, window))
                prediction = self._cached_prediction
        else:
            prediction = self._cached_prediction
            # Count ticks where we wanted fresh but kept stale.  Two reasons:
            # (a) refresh still in flight (slow KNN); (b) cache fresh enough.
            # Distinguish: inflight → genuine skip; not-stale → expected reuse.
            if stale and self._predict_task_inflight:
                self._predict_refresh_skipped += 1
        _mark("predict")
        # P3.0 Residual log: hold this prediction in a FIFO until its
        # horizon elapses, then write the (predicted, actual, residual)
        # tuple to disk for the meta-predictor to learn from.  The hot
        # path stays just an O(1) append + an O(k) sweep where k = number
        # of predictions whose horizon just elapsed (typically 0 or 1).
        # Validate against the latest sample ("now"), not the rolling-window
        # max — otherwise residuals compare a stuck peak against itself and
        # report fake 99% accuracy while the chip has long since cooled.
        self._validate_pending_predictions(
            now=frame.timestamp,
            current_actual=features.get("cpu_temp_now", features.get("cpu_temp_max")),
            current_features=features,
        )
        _mark("validate_pending")
        if prediction.expected_temp_c is not None:
            bucket_key: tuple[int, ...] | None = None
            if isinstance(self.predictor, MetaPredictor):
                bucket_key = self.predictor.bucket(features)
            self._pending_predictions.append((
                frame.timestamp,
                float(prediction.expected_temp_c),
                prediction.model_name,
                bucket_key,
                {k: float(v) for k, v in features.items() if isinstance(v, (int, float))},
            ))
        # P2.2: pass live KNN labelled-neighbour count into DecisionEngine so
        # the soft bias path (RAMP_COOLING) doesn't fire on a cold index.
        # Env hatches live in DecisionEngine (single source of truth) — daemon
        # only forwards data.
        # P2.3: live cpu_temp_c gates REDUCE_NOISE — the quiet-mode bias
        # only fires when the chip is *actually* below the knee right now,
        # not just predicted to stay calm. Reading from the current frame
        # keeps the safety belt independent of the predictor's accuracy.
        cpu_temp_now = (
            frame.cpu.temps_c.get("tctl") or frame.cpu.temps_c.get("tdie")
            if frame.cpu is not None else None
        )
        # Reload mode each tick — the dashboard's POST /api/mode writes
        # to runtime-state.json, daemon picks it up on the next tick
        # without restart. Read failures fall back to last-known value.
        _mark("pre_reload_mode")
        self._reload_mode_from_state()
        _mark("reload_mode")
        # Third defensive layer for quiet-mode: even if the predictor and
        # the decision engine both kept emitting REDUCE_NOISE, evict any
        # armed quiet bias the instant the chip crosses the eject ceiling.
        self._quiet_safety_eject(cpu_temp_c=cpu_temp_now)
        _mark("pre_decision")
        actions = self.decision.decide(
            prediction,
            calibration_ready=self.calibration_ready,
            labeled_count=self._labeled_count(),
            mode=self._mode,
            cpu_temp_c=cpu_temp_now,
        )
        _mark("after_decide")
        # P2.5 Heavy-3 — feed the segmenter every tick. The returned
        # SessionBoundary (when not None) is informative — we don't act
        # on it directly here, but the session_id is tagged on the
        # chroma write so future KNN queries can filter by session.
        try:
            boundary = self._segmenter.feed(frame)
            self._last_session_id = self._segmenter.current_session_id()
            if boundary is not None:
                log.debug(
                    "event_segmenter: boundary reason=%s session=%s",
                    boundary.reason, boundary.new_session_id,
                )
                # P2.9.7 fix (2026-05-13): residual bank holds per-bucket
                # EWMA bias from the *previous* workload.  When a load_jump
                # opens a new session, that bias is stale — feature vectors
                # of the fresh workload often land in the same coarse bucket
                # (4 axes × 3 levels = 81 cells) and inherit a correction
                # that was learned for a different regime.  Decay by 0.3
                # (more aggressive than tuned-profile flip at 0.5) so the
                # next ~10 validations re-converge.  focus_change and
                # plateau_collapse are gentler — load_jump is the strongest
                # regime-shift signal we have.
                if (
                    boundary.reason == "load_jump"
                    and isinstance(self.predictor, MetaPredictor)
                    and (time.time() - self._last_bank_decay_at) >= 30.0
                ):
                    self.predictor.bank.decay_all(0.3)
                    self._last_bank_decay_at = time.time()
                    log.info(
                        "event_segmenter: load_jump → residual bank decayed "
                        "(factor=0.3, buckets=%d)",
                        len(self.predictor.bank),
                    )
        except Exception as exc:  # noqa: BLE001
            log.debug("event_segmenter failed: %r", exc)
        # P2.4 — attach a curve-context payload to every curve-shaping
        # action so the asusctl actuator can route through the adaptive
        # policy pipeline instead of the legacy intensity_pct branch.
        # Workload class comes from the focused window when hyprctl is
        # available; recent_throttle_count is a rolling 1h sum.
        workload_class = frame.workload.label if frame.workload is not None else None
        top_processes = (
            [p.name for p in frame.workload.top_processes[:8]]
            if frame.workload is not None else []
        )
        # P2.5 Phase C — resolve into one of CODE/RENDER/GAME/IDLE/OTHER.
        # The .value goes into curve_ctx_payload so the `workload_profile_shape`
        # policy can branch per profile. Heat-soak index comes from
        # fingerprint.extract() (Heavy-1 added it to the features dict).
        from coolstep.core.workload_profile import (
            resolve_profile,  # local: avoids module-load cost when daemon doesn't fire actions
        )
        profile = resolve_profile(workload_class, top_processes)
        heat_soak_index = float(features.get("heat_soak_index", 0.0))
        recent_throttle = self._recent_throttle_count(now=time.time())
        armed_verbs = frozenset(
            a.verb.value for a, _ in self._armed_actions.values()
        )
        # P2.5 — Conservative Mode. KNN confidence below the threshold
        # AND any non-trivial heat hint → switch the predictive policy
        # to its softer, wider variant. Source of the threshold value
        # is `decision.Thresholds.conservative_floor` so the gate is
        # tunable in one place.
        conservative = (
            float(prediction.confidence) < self.decision.thresholds.conservative_floor
            and float(prediction.throttle_prob) > self.decision.thresholds.conservative_prob_hint
        )
        curve_ctx_payload = {
            "cpu_temp_c": cpu_temp_now,
            "throttle_prob": float(prediction.throttle_prob),
            "confidence": float(prediction.confidence),
            "expected_temp_c": prediction.expected_temp_c,
            "horizon_sec": float(prediction.horizon_sec),
            "mode": self._mode,
            "workload_class": workload_class,
            "recent_throttle_count": recent_throttle,
            "armed_verbs": tuple(armed_verbs),
            "conservative": conservative,
            # P2.5 — new context fields. The actuator extracts these into
            # CurveContext.workload_profile / .heat_soak_index for the
            # new policies (workload_profile_shape, heat_soak_bump).
            "workload_profile": profile.value,
            "heat_soak_index": heat_soak_index,
        }
        # P2.4 — cache the most recent predictor + telemetry snapshot so
        # the incident logger (triggered out-of-band by quiet_safety_eject
        # / throttle FSM close) can build an incident record without
        # re-running the prediction. Frozen at end of `decide()` because
        # that's when we have the complete picture for this tick.
        self._last_incident_snapshot = {
            "ts": time.time(),
            "cpu_temp_c": cpu_temp_now,
            "throttle_prob": float(prediction.throttle_prob),
            "confidence": float(prediction.confidence),
            "expected_temp_c": prediction.expected_temp_c,
            "reason": getattr(prediction, "reason", "") or "",
            "features": dict(features),
            "workload_class": workload_class,
            "top_processes": top_processes,
            "mode": self._mode,
            "armed_verbs": list(armed_verbs),
        }
        for action in actions:
            if action.verb in (ActionVerb.RAMP_COOLING, ActionVerb.REDUCE_NOISE):
                action.params["curve_context"] = dict(curve_ctx_payload)
            self._route_action(action)

        _mark("decision")
        self.ring.push(frame)
        self._labelled_window.append(frame)
        _mark("ring_push")
        # Skip the per-tick sqlite write while rotate() is holding the
        # writer lock (every 10 min, 2-5 s on a 250 MB store.db). Without
        # this gate the tick coroutine awaits inside the worker thread and
        # the next tick can't start until rotate finishes — visible to the
        # operator as a 2-5 s freeze on `ml-state.json` freshness.
        if not self.store.writes_paused:
            await asyncio.to_thread(self.store.write_frame, frame)
        _mark("store")
        # P2.9.6: chroma.add takes ~3s on a 42k-vector HNSW index.
        # Enqueue ~1 frame per 5 sec — index already at warm size (42k+),
        # missing intermediate ticks costs ~zero training quality but
        # keeps the drain worker from holding the GIL on the hot path.
        chroma_write_every = max(1, int(round(5.0 / self.period)))
        if self.chroma.available and self._tick_count % chroma_write_every == 0:
            try:
                self._chroma_queue.put_nowait(frame)
            except asyncio.QueueFull:
                try:
                    self._chroma_queue.get_nowait()
                    self._chroma_queue.put_nowait(frame)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        self._throttle_fsm_tick(frame)
        _mark("chroma_enqueue_fsm")
        # P2.1: persist on every tick so a hard crash leaves at most one
        # tick's worth of staleness in armed_actions — next start reverts
        # within `period_sec` of restart instead of forever.
        self._persist_runtime_state()
        _mark("persist_runtime")

        if (self._tick_count - self._backfill_last_tick) >= self._backfill_interval_ticks:
            if (
                self._backfill_inflight_task is None
                or self._backfill_inflight_task.done()
            ):
                task = asyncio.create_task(
                    asyncio.to_thread(
                        backfill_labels, self.store.path, self.chroma
                    )
                )
                task.add_done_callback(_log_backfill_exception)
                self._backfill_inflight_task = task
                self._backfill_last_tick = self._tick_count

        # ml-state.json drives the live dashboard predictor dot.  Every tick
        # — atomic write of ~3 KB JSON on an NVMe is <1ms.  Previously this
        # ran every 30 ticks (disk-write economy), but it meant the frontend
        # saw a `expected_temp_c` stale by up to 30 s, and the predicted dot
        # appeared "frozen" relative to live in spite of frontend correctly
        # polling 1 Hz.  The frontend frozen-lock discipline (5 s horizon)
        # is what should freeze the dot, not stale disk.
        await asyncio.to_thread(self._dump_ml_state, prediction, features)
        _mark("dump_ml_state")
        # P2.9.6: with daemon at 10Hz, time-based intervals must be
        # multiplied by 10 to keep their original calendar cadence.
        # Helper: ticks_for(seconds) = seconds / self.period (rounded).
        ticks_per_sec = max(1, int(round(1.0 / self.period)))
        every_30s = ticks_per_sec * 30
        every_5min = ticks_per_sec * 300
        every_10min = ticks_per_sec * 600
        every_min = ticks_per_sec * 60

        # Tick-stage timings — sampled every minute for observability.
        if self._tick_count % every_min == 0 and self._tick_count > 0:
            log.info("tick %d stages (ms): %s", self._tick_count, _stage_t)
        # Slow-tick alarm: anything over 500ms is a freshness-gap candidate.
        # Logs the full stage map so the operator (and post-mortem) can see
        # which marker carried the cost.
        tick_total_ms = (time.monotonic() - _t0) * 1000
        if tick_total_ms > 500.0:
            log.warning(
                "slow tick %d: %.0fms total — stages: %s",
                self._tick_count, tick_total_ms, _stage_t,
            )
        if self._tick_count % every_30s == 0:
            await asyncio.to_thread(self._refit_embedder)
            # Profile-flip detector.  When /etc/tuned/active_profile changes,
            # decay the residual bank by 0.5 so the next ~10-20 validations
            # re-converge to the new thermal envelope.  Skip the very first
            # poll (boot-time observation, not a transition).
            new_profile = self._profile_watcher.poll(now=time.time())
            if (
                new_profile is not None
                and not self._profile_watcher.is_first_poll
                and self._tick_count > 0
                and isinstance(self.predictor, MetaPredictor)
            ):
                self.predictor.bank.decay_all(0.5)
                self._tuned_profile_changed_at = time.time()
                log.info(
                    "tuned profile flip: %s — residual bank decayed (factor=0.5, buckets=%d)",
                    new_profile, len(self.predictor.bank),
                )

        if self._tick_count % every_30s == 0 and self._tick_count > 0:
            # Fire-and-forget with inflight guard. The worker iterates a
            # snapshot of `_labelled_window` and pushes chroma metadata
            # updates (1-6 s on a deep buffer); the done-callback trims
            # the live deque from the event-loop thread to keep mutations
            # single-threaded. Skipping a firing while inflight just
            # delays the trim by 30 s — newer frames pile up briefly,
            # next pass processes them.
            if (
                self._backfill_labels_inflight_task is None
                or self._backfill_labels_inflight_task.done()
            ):
                task = asyncio.create_task(
                    asyncio.to_thread(self._backfill_labels)
                )
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
                task.add_done_callback(self._on_backfill_labels_done)
                self._backfill_labels_inflight_task = task

        # P2.9.6 phase 3: refresh cached chroma stats off the hot path.
        # Every 30s — same as backfill.  Walks HNSW + persist dir.
        if (
            self.chroma.available
            and self._tick_count > 0
            and self._tick_count % every_30s == 0
        ):
            asyncio.create_task(self._refresh_chroma_stats())

        if self._tick_count % every_5min == 0 and self._tick_count > 0:
            await asyncio.to_thread(self._append_drift_history)

        if self._tick_count % every_10min == 0 and self._tick_count > 0:
            # Fire-and-forget: rotate's bulk DELETE pass can hold the
            # sqlite writer lock for 2-5 s on a 250 MB store.db, and the
            # chroma persist-dir walk adds another 100-500 ms. Awaiting
            # both inline would pin the tick coroutine — and therefore
            # the next tick's `dump_ml_state` — for the full duration.
            # `Store._writes_paused` gates per-tick `write_frame` so the
            # rotate's DELETE doesn't get queued behind a tick.
            rotate_task = asyncio.create_task(
                asyncio.to_thread(self.store.rotate)
            )
            self._bg_tasks.add(rotate_task)
            rotate_task.add_done_callback(self._bg_tasks.discard)
            rotate_task.add_done_callback(_log_rotate_exception)
            guard_task = asyncio.create_task(
                asyncio.to_thread(self._chroma_size_guard)
            )
            self._bg_tasks.add(guard_task)
            guard_task.add_done_callback(self._bg_tasks.discard)
            guard_task.add_done_callback(_log_chroma_guard_exception)

        # P2.8+ drift-triggered embedder refit check.  Runs every
        # COOLSTEP_REFIT_CHECK_HOURS hours (default 6).  Uses wall-clock
        # so it's immune to tick-rate changes (daemon tuning, sleep).
        # Runs in a thread — detect_cluster_drift + refit_and_swap are sync
        # and can take several seconds.
        if (
            self.chroma.available
            and self._tick_count > 0
            and not self._refit_inflight
            and (time.time() - self._last_refit_check_at)
                >= self._refit_check_hours * 3600.0
        ):
            self._last_refit_check_at = time.time()
            self._refit_inflight = True
            asyncio.create_task(asyncio.to_thread(self._guarded_refit_check))

        # P2.5 Heavy-3 — efficiency calibration. Scans the last 10 min
        # of frames in the ring (no sqlite query — Ring is in-memory and
        # cheap), buckets stable runs by (workload, power, ambient),
        # appends min-RPM rows to data/efficiency_table.jsonl. Best-
        # effort; failures are logged and ignored.
        if (
            self._tick_count - self._last_efficiency_at_tick
            >= self._efficiency_analyse_every_ticks
        ) and self._tick_count > 0:
            self._last_efficiency_at_tick = self._tick_count
            await asyncio.to_thread(self._run_efficiency_pass)

        if self._tick_count % 60 == 0:
            await asyncio.to_thread(self._refresh_calibration)

    def _recover_orphan_labels(self) -> int:
        """Re-label chroma vectors that missed backfill (daemon restart while
        their +30s lookahead was pending). Default → COOL: they were active
        in `_labelled_window` at shutdown, не triggered throttle FSM (which
        persists separately), значит idle/cool baseline. Without this any
        unlabelled vector stays LABEL_UNKNOWN forever and is excluded by
        `labeled_only=True` in KnnPredictor (predictor-audit risk #6)."""
        if not self.chroma.available:
            return 0
        from coolstep.core.schema import LABEL_COOL
        cutoff = time.time() - 60.0  # 2× LOOKAHEAD_SEC = grace window
        try:
            orphans = self.chroma.list_unlabeled(before_ts=cutoff, limit=2000)
        except Exception as exc:  # noqa: BLE001
            log.warning("orphan label scan failed (%s): %r", type(exc).__name__, exc)
            return 0
        if not orphans:
            return 0
        recovered = 0
        for o in orphans:
            ts = o.get("ts")
            meta = dict(o.get("metadata") or {})
            meta["was_hot_in_30s"] = LABEL_COOL
            meta["peak_temp_after"] = float(meta.get("cpu_temp_at", 0.0))
            try:
                self.chroma.update_metadata(float(ts), meta)
                recovered += 1
            except Exception:  # noqa: BLE001
                pass
        if recovered:
            log.info("recovered %d orphan labels → LABEL_COOL", recovered)
        return recovered

    def _restore_runtime_state(self) -> None:
        """Восстановить throttle FSM + backfill cursor из runtime-state.json
        на старте. Без этого SIGTERM теряет открытый «hot» episode (frame'ы
        с LABEL_UNKNOWN остаются в Chroma forever)."""
        if not self._runtime_state_path.exists():
            return
        try:
            data = json.loads(self._runtime_state_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("runtime-state restore failed (%s): %r", type(exc).__name__, exc)
            return
        self._throttle_state = data.get("throttle_state", "idle")
        self._throttle_start_ts = float(data.get("throttle_start_ts", 0.0))
        self._throttle_peak_temp = float(data.get("throttle_peak_temp", 0.0))
        self._throttle_workload_at_start = data.get("throttle_workload_at_start")
        self._backfill_cursor_ts = float(data.get("backfill_cursor_ts", 0.0))
        mode = str(data.get("mode") or DEFAULT_MODE).lower()
        self._mode = mode if mode in VALID_MODES else DEFAULT_MODE
        if self._throttle_state == "hot":
            log.info(
                "runtime-state: resuming hot episode start=%.1f peak=%.1f",
                self._throttle_start_ts, self._throttle_peak_temp,
            )

    def _persist_runtime_state(self) -> None:
        """Сохранить FSM + backfill cursor + armed_actions. Зовётся per-tick
        (см. _tick) и на FSM transitions — IO дешёвый (~200B JSON), а
        actuator-revert-on-crash зависит от свежести armed_actions field."""
        try:
            data = {
                "throttle_state": self._throttle_state,
                "throttle_start_ts": self._throttle_start_ts,
                "throttle_peak_temp": self._throttle_peak_temp,
                "throttle_workload_at_start": self._throttle_workload_at_start,
                "backfill_cursor_ts": self._backfill_cursor_ts,
                "mode": self._mode,
                "armed_actions": [
                    {
                        "actuator": name,
                        "verb": action.verb.value,
                        "expires_at": float(action.expires_at),
                        "applied_at": float(applied_at),
                    }
                    for name, (action, applied_at) in self._armed_actions.items()
                ],
                "saved_at": time.time(),
            }
            self._runtime_state_path.write_text(json.dumps(data, indent=2))
        except OSError as exc:
            log.warning("runtime-state persist failed (%s): %r", type(exc).__name__, exc)

    def _do_apply(self, actuator: Actuator, action: Action) -> ActionResult | None:
        """Apply `action` to a SPECIFIC `actuator`, honour re-arm guard, track armed.

        Lower-level than `_route_action` (which iterates and picks). Tests and
        external callers can drive a single actuator without the routing dance.
        Returns the ActionResult, or None if the re-arm guard blocked.
        """
        now = time.time()
        if self._should_skip_rearm(actuator, now=now):
            existing = self._armed_actions[actuator.name][0]
            log.debug(
                "%s skip apply: re-arm gap (existing expires_at=%.1f, gap=%.1fs)",
                actuator.name, existing.expires_at, REARM_GAP_SEC,
            )
            return None
        result = actuator.apply(action)
        if (
            actuator.name not in _NO_REVERT_ACTUATORS
            and getattr(result, "error", None) is None
        ):
            self._armed_actions[actuator.name] = (action, now)
            # Persist immediately so a daemon crash between apply() and the next
            # tick still leaves the armed state on disk for crash-recovery.
            self._persist_runtime_state()
        return result

    def _route_action(self, action: Action) -> None:
        """Route action to all supporting actuators with audit-first semantics.

        Two passes, both first-match:

        1. **Audit pass.** Find the `readonly_log` instance (if present) and
           call `apply()` for journal'ing. This is NOT tracked in
           `_armed_actions` — readonly is in `_NO_REVERT_ACTUATORS` so there
           is no TTL/revert lifecycle. Failures are swallowed (debug log) —
           audit must never crash the daemon.

        2. **Hardware pass.** Pick the FIRST non-readonly actuator whose
           `supports(verb) == True` and route through `_do_apply`, which
           honours the re-arm guard and tracks the armed entry. Only one
           hardware fire per action — don't double-bias.

        Bug history: before this dual-pass, `_route_action` stopped at the
        first supporting actuator. Because `readonly_log.supports(*) == True`,
        if it appeared earlier in discovery order it starved every hardware
        actuator (asusctl_fan_curve, epp_shift, …). The audit-only path
        gave the illusion of action without ever moving the hardware.

        If no hardware actuator supports the verb, the action stays as
        audit-only — that's the correct behaviour for hosts where the
        hardware adapter didn't discover (e.g. asusctl missing).
        """
        # Audit pass — readonly_log gets every verb, no tracking.
        for actuator in self.actuators:
            if actuator.name != "readonly_log":
                continue
            try:
                actuator.apply(action)
            except Exception as exc:  # noqa: BLE001
                log.debug("readonly audit failed: %r", exc)
            break

        # Hardware pass — first non-audit actuator that supports the verb wins.
        for actuator in self.actuators:
            if actuator.name in _NO_REVERT_ACTUATORS:
                continue
            if not actuator.supports(action.verb):
                continue
            self._do_apply(actuator, action)
            return

    def _ttl_sweep(self, now: float) -> None:
        """Public alias for `_sweep_expired_armed` (P2.1 spec name).

        Kept under both names: `_sweep_expired_armed` is the original
        descriptive impl name; `_ttl_sweep` is the contract name tests
        and external sequencers reference.
        """
        self._sweep_expired_armed(now=now)

    def _shutdown_hook(self) -> None:
        """Public alias for `_revert_all_armed("shutdown")`.

        Called once at daemon teardown — `run()` already invokes
        `_revert_all_armed("shutdown")` in its finally; this thin wrapper
        lets tests and external orchestrators trigger the same path
        without depending on the internal reason string.
        """
        self._revert_all_armed("shutdown")

    def _quiet_safety_eject(self, cpu_temp_c: float | None) -> None:
        """Hard-revert any armed REDUCE_NOISE action if the chip is hot.

        The third defensive layer for quiet mode (after the
        DecisionEngine gates and the actuator's own clamps). Runs on every
        tick and inspects `action.params["eject_temp_c"]` per-action so
        the threshold travels with the decision — if the policy widens,
        the safety belt widens with it. `cpu_temp_c is None` is treated
        as «not safe to hold quiet bias» — eject anyway.

        P2.4 — every eject also writes a `quiet_eject` incident with a
        multi-angle similarity verdict against prior incidents. Done
        BEFORE revert so the snapshot reflects «what the chip looked
        like when quiet failed», not «what it looked like after the
        actuator restored baseline».
        """
        if not self._armed_actions:
            return
        for name in list(self._armed_actions.keys()):
            action, _applied_at = self._armed_actions[name]
            if action.verb != ActionVerb.REDUCE_NOISE:
                continue
            eject_at = float(action.params.get("eject_temp_c", 80.0))
            if cpu_temp_c is not None and cpu_temp_c < eject_at:
                continue
            # P2.4 — incident write before revert.
            try:
                self._write_incident(
                    kind="quiet_eject",
                    peak_temp_c=float(cpu_temp_c) if cpu_temp_c is not None else 0.0,
                    duration_s=max(0.0, time.time() - float(_applied_at)),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("incident write (quiet_eject) failed: %r", exc)
            actuator = self._find_actuator(name)
            reason = (
                f"quiet_safety_eject Tctl={cpu_temp_c}°C >= {eject_at}°C"
                if cpu_temp_c is not None
                else "quiet_safety_eject (Tctl unknown)"
            )
            if actuator is not None:
                try:
                    actuator.revert()
                except Exception as exc:  # noqa: BLE001
                    log.warning("actuator_revert failed: name=%s reason=%s exc=%r",
                                name, reason, exc)
                else:
                    log.info("actuator_revert: name=%s reason=%s", name, reason)
            self._armed_actions.pop(name, None)

    def _write_incident(self, *, kind: str, peak_temp_c: float,
                        duration_s: float) -> None:
        """Build an Incident from the cached snapshot, attach a
        multi-angle similarity verdict, and append to incidents.jsonl.

        Best-effort by design — wrapped in try/except at the caller so
        the trigger path (eject / throttle close) never blocks on this.
        """
        from coolstep.core.incidents import (
            Incident,
            find_similar,
            log_incident,
        )
        snap = self._last_incident_snapshot or {}
        if not snap:
            # First-tick edge case — no snapshot yet. Still log a
            # minimal incident with what we have, so the user sees
            # _something_ when this rare path fires.
            snap = {"ts": time.time(), "mode": self._mode}
        # Try to embed the current frame for the feature-vector angle.
        embedding: list[float] | None = None
        try:
            if self.embedder.fitted() and self._labelled_window:
                emb = self.embedder.embed(self._labelled_window[-1])
                if emb is not None:
                    embedding = list(emb)
        except Exception:  # noqa: BLE001
            embedding = None
        incident = Incident(
            ts=time.time(),
            kind=kind,
            peak_temp_c=float(peak_temp_c),
            duration_s=float(duration_s),
            workload_class=snap.get("workload_class"),
            top_processes=list(snap.get("top_processes") or []),
            predictor_state={
                "throttle_prob": snap.get("throttle_prob"),
                "confidence": snap.get("confidence"),
                "expected_temp_c": snap.get("expected_temp_c"),
                "reason": snap.get("reason"),
            },
            features={k: float(v) for k, v in (snap.get("features") or {}).items()
                      if isinstance(v, (int, float))},
            mode_at_incident=str(snap.get("mode") or self._mode),
            armed_verbs_at_incident=list(snap.get("armed_verbs") or []),
            embedding=embedding,
        )
        incident.similar = find_similar(incident.to_dict(), top_k_per_angle=3)
        log_incident(incident)

    def _write_spike_incident(self, spike_record: Any) -> None:
        """Persist a closed SpikeRecord as Incident(kind="predictor_spike").

        Why: the operator framing is that a spike *is* an incident — a
        moment the predictor was surprised by a real workload step.
        Routing it through the existing incident journal means the
        multi-angle similarity search already covers it: next time a
        speedtest / steam-launch / zen-fetch fingerprint hits, the
        ``feature_vector`` angle finds the closed spike as a neighbour
        and the operator sees "we have seen this shape before".

        Best-effort — wrapped at caller; logs at debug on failure."""
        from coolstep.core.incidents import (
            Incident,
            find_similar,
            log_incident,
        )
        # Embed the *current* frame as the spike's feature vector — at
        # closure the chip has stabilised and that's the signature
        # downstream code will match against future workloads.
        embedding: list[float] | None = None
        try:
            if self.embedder.fitted() and self._labelled_window:
                emb = self.embedder.embed(self._labelled_window[-1])
                if emb is not None:
                    embedding = list(emb)
        except Exception:  # noqa: BLE001
            embedding = None
        incident = Incident(
            ts=float(spike_record.ended_at),
            kind="predictor_spike",
            peak_temp_c=float(spike_record.peak_temp_c),
            duration_s=float(spike_record.duration_s),
            workload_class=spike_record.workload_label,
            top_processes=list(
                (self._last_incident_snapshot or {}).get("top_processes") or []
            ),
            predictor_state={
                "model": spike_record.predictor_model,
                "max_abs_residual": float(spike_record.max_abs_residual),
                "n_validations": int(spike_record.n_validations),
            },
            features=dict(spike_record.started_features),
            mode_at_incident=str(self._mode),
            armed_verbs_at_incident=list(
                (self._last_incident_snapshot or {}).get("armed_verbs") or []
            ),
            embedding=embedding,
        )
        try:
            incident.similar = find_similar(incident.to_dict(), top_k_per_angle=3)
        except Exception:  # noqa: BLE001
            incident.similar = []
        log_incident(incident)
        log.info(
            "predictor_spike closed: dur=%.1fs max|res|=%.2f°C workload=%s model=%s",
            spike_record.duration_s,
            spike_record.max_abs_residual,
            spike_record.workload_label,
            spike_record.predictor_model,
        )

    def _reload_mode_from_state(self) -> None:
        """Pick up `mode` field from runtime-state.json (cheap, ~200B JSON).

        The dashboard's POST /api/mode endpoint writes to the same file,
        so the daemon converges to user intent within one tick of the
        request. Decoupling via the state file (instead of an IPC socket)
        avoids adding another transport surface — the file is already
        being read/written every tick for throttle FSM + armed_actions.
        Read failures are silent: we fall back to the last cached mode.
        """
        try:
            text = self._runtime_state_path.read_text()
        except OSError:
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return
        new_mode = str(data.get("mode") or DEFAULT_MODE).lower()
        if new_mode not in VALID_MODES:
            new_mode = DEFAULT_MODE
        if new_mode != self._mode:
            log.info("mode flip: %s → %s (from runtime-state.json)", self._mode, new_mode)
            self._mode = new_mode

    def _validate_pending_predictions(
        self,
        now: float,
        current_actual: float | None,
        current_features: dict[str, float],
    ) -> None:
        """Drain the pending-prediction FIFO: any entry whose horizon has
        elapsed is validated against `current_actual` and appended to the
        residual log.  Entries are added in chronological order, so we can
        slice the FIFO from the head until we hit one not yet ready."""
        if current_actual is None or not self._pending_predictions:
            return
        from coolstep.core.residual_log import ResidualRecord
        horizon = float(getattr(self.predictor, "horizon_sec", 5.0))
        idx = 0
        for predicted_at, predicted_temp, model_name, bucket_key, feat_snapshot in self._pending_predictions:
            if now - predicted_at < horizon:
                break  # everything past this is too young; FIFO is chronological
            try:
                residual = float(current_actual) - predicted_temp
                record = ResidualRecord.from_pair(
                    ts=now,
                    predicted_at=predicted_at,
                    horizon_sec=horizon,
                    predicted_temp_c=predicted_temp,
                    actual_temp_c=float(current_actual),
                    model_name=model_name,
                    profile=self._active_tuned_profile(),
                    bucket_key=bucket_key,
                    features=feat_snapshot,
                )
                self.residual_log.append(record)
                # Live feed back into the meta-predictor's bank — this is
                # the closed loop.  Without it the bank would only see
                # restart-time replays and never learn during the session.
                if isinstance(self.predictor, MetaPredictor) and bucket_key is not None:
                    self.predictor.bank.observe_at_bucket(
                        tuple(bucket_key),  # type: ignore[arg-type]
                        residual,
                    )
                # Spike pass: every validated residual feeds the
                # detector.  A closed spike → Incident(kind=predictor_spike)
                # so the multi-angle similarity search picks it up next
                # time a workload of the same shape arrives.  Worth
                # noting: the spike's `started_features` are the
                # predictor's features at horizon-back, not now — that's
                # the right snapshot for "what conditions opened this
                # surprise".
                try:
                    spike_record = self.spike_detector.update(
                        ts=now,
                        residual_c=residual,
                        peak_temp_c=float(current_actual),
                        workload_label=(
                            self._last_incident_snapshot or {}
                        ).get("workload_class"),
                        features=feat_snapshot,
                        predictor_model=model_name,
                    )
                    if spike_record is not None:
                        # `_write_spike_incident` runs `find_similar` (multi-
                        # angle KNN over Chroma) + `log_incident` (jsonl
                        # append). On a 42k-vector index the KNN sweep is
                        # 80-250 ms — the dominant tail in tick freezes
                        # operator described as "тикает, потом думает".
                        # Fire-and-forget on a worker thread keeps the
                        # tick coroutine responsive; ordering on
                        # incidents.jsonl is preserved by POSIX small-append
                        # atomicity and spike closures are rare (~1/min
                        # at peak), so we never pile up.
                        try:
                            loop = asyncio.get_running_loop()
                            task = loop.create_task(
                                asyncio.to_thread(
                                    self._write_spike_incident, spike_record
                                )
                            )
                            self._bg_tasks.add(task)
                            task.add_done_callback(self._bg_tasks.discard)
                            task.add_done_callback(_log_spike_incident_exception)
                        except RuntimeError:
                            # Not on a loop (e.g. unit test) — execute inline.
                            self._write_spike_incident(spike_record)
                except Exception as exc:  # noqa: BLE001
                    log.debug("spike detector update failed: %r", exc)
            except Exception as exc:  # noqa: BLE001
                log.debug("residual_log append failed: %r", exc)
            idx += 1
        if idx > 0:
            self._pending_predictions = self._pending_predictions[idx:]

    def _active_tuned_profile(self) -> str | None:
        """Read /etc/tuned/active_profile if present.  Phase 4 will cache
        + watch this; Phase 1 just reads on every validation (cheap, ~1ms
        every horizon_sec at most).  Robust against the file not existing
        on non-tuned hosts."""
        try:
            with open("/etc/tuned/active_profile", encoding="utf-8") as f:
                return f.read().strip() or None
        except (FileNotFoundError, PermissionError, OSError):
            return None

    def _sweep_expired_armed(self, now: float) -> None:
        """Revert actuators whose TTL has expired (action.expires_at + grace).

        Best-effort: a failing `revert()` is logged and dropped from the
        armed set anyway — leaving it armed would mean re-trying forever and
        could amplify a hardware fault. Bias toward "release the bias".
        """
        if not self._armed_actions:
            return
        for name in list(self._armed_actions.keys()):
            action, _applied_at = self._armed_actions[name]
            if now <= action.expires_at + TTL_GRACE_SEC:
                continue
            actuator = self._find_actuator(name)
            age = now - action.expires_at
            if actuator is not None:
                try:
                    actuator.revert()
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "actuator_revert failed: name=%s reason=ttl_expired age=%.1f exc=%r",
                        name, age, exc,
                    )
                else:
                    log.info(
                        "actuator_revert: name=%s reason=ttl_expired age=%.1f",
                        name, age,
                    )
            else:
                log.warning(
                    "actuator_revert skipped: name=%s not in discovery (drop arm)",
                    name,
                )
            self._armed_actions.pop(name, None)

    def _should_skip_rearm(self, actuator, now: float) -> bool:  # type: ignore[no-untyped-def]
        """Return True if `actuator` was applied recently enough that we
        should not stack a fresh apply on top. Threshold: existing entry's
        `expires_at - REARM_GAP_SEC` is still in the future.
        """
        entry = self._armed_actions.get(actuator.name)
        if entry is None:
            return False
        existing_action, _applied_at = entry
        return now < existing_action.expires_at - REARM_GAP_SEC

    def _find_actuator(self, name: str):  # type: ignore[no-untyped-def]
        for a in self.actuators:
            if a.name == name:
                return a
        return None

    def _recover_armed_actions(self) -> None:
        """On startup, restore `armed_actions` from runtime-state.json and
        revert anything that's already expired or about to expire (grace +60s).
        Then drop them so the freshly-loaded state matches an "all clear"
        baseline — the next persist will rewrite an empty list.
        """
        if not self._runtime_state_path.exists():
            return
        try:
            data = json.loads(self._runtime_state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        armed = data.get("armed_actions") or []
        if not armed:
            return
        now = time.time()
        recovered = 0
        for entry in armed:
            name = entry.get("actuator")
            expires_at = float(entry.get("expires_at", 0.0))
            if not name or expires_at >= now + 60.0:
                # Still well within TTL — treat as live; rebuild armed map
                # so the per-tick sweep handles it normally.
                verb_raw = entry.get("verb", "")
                try:
                    verb = ActionVerb(verb_raw)
                except ValueError:
                    continue
                applied_at = float(entry.get("applied_at", now))
                self._armed_actions[name] = (
                    Action(verb=verb, params={}, expires_at=expires_at),
                    applied_at,
                )
                continue
            actuator = self._find_actuator(name)
            if actuator is None:
                log.info(
                    "armed-recovery: actuator %s not present, skip revert", name,
                )
                continue
            try:
                actuator.revert()
                recovered += 1
                log.info(
                    "armed-recovery: reverted %s (expired_at=%.1f, age=%.1fs)",
                    name, expires_at, now - expires_at,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "armed-recovery: revert %s failed: %r", name, exc,
                )
        # Persist unconditionally when there were any entries to process —
        # entries that got rebuilt are still in `_armed_actions` and re-persist
        # naturally; entries that got reverted OR were skipped (actuator
        # missing) are now absent and the on-disk list must reflect that.
        # Without this, a stale entry whose actuator isn't in discovery would
        # haunt runtime-state.json forever.
        self._persist_runtime_state()

    def _throttle_fsm_tick(self, frame: TelemetryFrame) -> None:
        """Update throttle state per frame; emit `write_throttle_event` on
        episode close. Calibration gate `throttle_events` reads that table."""
        cpu_temp = (
            frame.cpu.temps_c.get("tctl")
            or frame.cpu.temps_c.get("tdie")
            or 0.0
        )
        if cpu_temp <= 0.0:
            return  # без достоверной температуры FSM не двигаем
        if self._throttle_state == "idle":
            if cpu_temp >= THROTTLE_ENTER_C:
                self._throttle_state = "hot"
                self._throttle_start_ts = frame.timestamp
                self._throttle_peak_temp = cpu_temp
                self._throttle_workload_at_start = (
                    frame.workload.label if frame.workload else None
                )
                self._persist_runtime_state()  # capture episode start
        else:  # state == "hot"
            self._throttle_peak_temp = max(self._throttle_peak_temp, cpu_temp)
            if cpu_temp <= THROTTLE_EXIT_C:
                duration = frame.timestamp - self._throttle_start_ts
                if duration >= THROTTLE_MIN_DURATION_S:
                    cause = (
                        f"thermal_pressure_{int(self._throttle_peak_temp)}c"
                    )
                    try:
                        self.store.write_throttle_event(
                            ts_start=self._throttle_start_ts,
                            ts_end=frame.timestamp,
                            peak_temp=self._throttle_peak_temp,
                            cause_label=cause,
                            workload_at_start=self._throttle_workload_at_start,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "throttle write failed (%s): %r",
                            type(exc).__name__, exc,
                        )
                    # P2.4 — incident parallel to the sqlite row. Same
                    # snapshot pattern as _quiet_safety_eject; carries
                    # the multi-angle similarity verdict so subsequent
                    # close episodes know whether this one «looks like»
                    # the prior ones.
                    try:
                        self._write_incident(
                            kind="throttle_close",
                            peak_temp_c=float(self._throttle_peak_temp),
                            duration_s=float(duration),
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning("incident write (throttle_close) failed: %r", exc)
                self._throttle_state = "idle"
                self._throttle_start_ts = 0.0
                self._throttle_peak_temp = 0.0
                self._throttle_workload_at_start = None
                self._persist_runtime_state()  # capture episode close

    async def _sample_with_timeout(self, collector) -> dict[str, object] | None:  # type: ignore[no-untyped-def]
        timeout = COLLECTOR_TIMEOUTS.get(collector.name, DEFAULT_COLLECTOR_TIMEOUT)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(collector.sample),
                timeout=timeout,
            )
        except (TimeoutError, Exception) as exc:  # noqa: BLE001
            # str(TimeoutError()) = '' — без type(exc).__name__ лог пустой.
            log.warning(
                "collector %s failed (%s): %r",
                collector.name, type(exc).__name__, exc,
            )
            return None

    def _run_efficiency_pass(self) -> None:
        """Analyse the trailing-10min frame ring for stable runs and
        append minimum-RPM buckets to data/efficiency_table.jsonl.

        Pure read on `self.ring` + write on a separate jsonl file —
        doesn't touch sqlite, chroma, or the actuator. Best-effort:
        ImportError / empty ring / OSError all log at debug + return.
        """
        try:
            from coolstep.core.efficiency_calibration import (
                analyse_window,
                append_table,
            )
        except ImportError as exc:
            log.debug("efficiency pass skipped (import): %r", exc)
            return
        frames = list(self.ring.window(600))  # ≤ 10 min at 1Hz
        if not frames:
            return
        try:
            rows = analyse_window(frames)
            if rows:
                append_table(rows)
                log.info("efficiency: %d new rows appended", len(rows))
        except Exception as exc:  # noqa: BLE001
            log.debug("efficiency pass failed: %r", exc)

    def _append_drift_history(self) -> None:
        if not self.ml_state_path.exists():
            return
        try:
            snap = json.loads(self.ml_state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        history_path = self.ml_state_path.parent / "drift-history.jsonl"
        drift_mod.append_history(history_path, snap)

    def _refresh_calibration(self) -> None:
        report = eval_calibration(store_path=self.store.path, ring=self.ring)
        self.calibration_ready = report.ready

    def _refit_embedder(self) -> None:
        """Refresh per-feature stats over the rolling ring window.

        Locked when stats loaded from disk on boot — иначе daemon
        rewrite'нет статистику reindex-скрипта (всего архива) более
        узкой ring window (~10 min) и embeddings разъедутся с уже
        существующими в chroma vectors. Unlock = удалить embedder-stats.json.
        """
        if self._embedder_stats_locked:
            return
        window = self.ring.window(self.ring.capacity)
        if len(window) >= self.embedder.min_frames_to_fit:
            self.embedder.refit(window)
            try:
                self.embedder.save_stats(self._embedder_stats_path)
            except OSError as exc:
                log.warning("embedder stats persist failed: %r", exc)

    def _chroma_write(self, frame: TelemetryFrame) -> None:
        if not self.chroma.available:
            return
        vector = self.embedder.embed(frame)
        if vector is None:
            return
        # Skip when temperature sensors disappeared (linux_sysfs flap).
        # Otherwise cpu_temp_at=0.0 would land in Chroma metadata and
        # corrupt the lookahead training set (collectors-audit BLOCKER #2).
        cpu_temp_raw = frame.cpu.temps_c.get("tctl") or frame.cpu.temps_c.get("tdie")
        if cpu_temp_raw is None or cpu_temp_raw <= 0.0:
            return
        cpu_temp = cpu_temp_raw
        gpu_temps = [g.temp_c for g in frame.gpus if g.temp_c is not None]
        gpu_temp = max(gpu_temps) if gpu_temps else 0.0
        fan_rpms = [f.rpm for f in frame.fans if f.rpm is not None]
        fan_max = max(fan_rpms) if fan_rpms else 0
        label = ""
        if frame.workload and frame.workload.label:
            label = str(frame.workload.label)
        meta = {
            "ts": float(frame.timestamp),
            "cpu_temp_at": float(cpu_temp),
            "gpu_temp_at": float(gpu_temp),
            "fan_max_at": int(fan_max),
            "workload_label": label,
            "was_hot_in_30s": LABEL_UNKNOWN,  # backfilled after lookahead
            "peak_temp_after": -1.0,
        }
        self.chroma.add(frame.timestamp, vector, meta)

    def _backfill_labels(self) -> float | None:
        """Walk the labelled-window buffer, label frames whose +30s lookahead
        has passed, push metadata back to ChromaDB.

        Returns the trim cutoff (now - 2·LOOKAHEAD_SEC) when work was done,
        or None if there was nothing to process. The caller is responsible
        for trimming `_labelled_window` on the event-loop thread — this
        method MUST NOT mutate `_labelled_window` because it now runs in a
        worker thread while the tick coroutine keeps appending new frames.
        """
        if not self.chroma.available or not self._labelled_window:
            return None
        now = time.time()
        # Snapshot the deque: list(...) takes a shallow copy, immune to
        # concurrent .append() from the tick. We process the snapshot only.
        snapshot = list(self._labelled_window)
        buf_temps: list[tuple[float, float]] = [
            (
                f.timestamp,
                f.cpu.temps_c.get("tctl") or f.cpu.temps_c.get("tdie") or 0.0,
            )
            for f in snapshot
        ]
        for f in snapshot:
            if f.timestamp + LOOKAHEAD_SEC > now:
                continue
            future_temps = [
                t for ts, t in buf_temps
                if f.timestamp <= ts <= f.timestamp + LOOKAHEAD_SEC
            ]
            peak = max(future_temps) if future_temps else 0.0
            label = LABEL_HOT if peak >= HOT_THRESHOLD_C else LABEL_COOL
            self.chroma.update_metadata(
                f.timestamp,
                {"was_hot_in_30s": label, "peak_temp_after": float(peak)},
            )
        return now - 2 * LOOKAHEAD_SEC

    def _trim_labelled_window(self, cutoff: float) -> None:
        """Drop entries older than `cutoff` from `_labelled_window`.

        Runs on the event-loop thread (called from `_on_backfill_labels_done`
        done-callback). Frames the chroma update visited are typically older
        than cutoff; newer frames the tick appended during the worker pass
        survive unchanged.
        """
        self._labelled_window = [
            f for f in self._labelled_window if f.timestamp >= cutoff
        ]

    def _on_backfill_labels_done(self, task: "asyncio.Task[Any]") -> None:
        if task.cancelled():
            return
        try:
            cutoff = task.result()
        except Exception as exc:  # noqa: BLE001
            log.debug("_backfill_labels failed: %r", exc)
            return
        if cutoff is not None:
            self._trim_labelled_window(cutoff)

    def _chroma_size_guard(self) -> None:
        """Watchdog for chroma persist dir bloat (incident 2026-05-04).

        Background: a 30-min run produced 98 GB link_lists.bin on chroma 1.5.8.
        Couldn't reproduce on a fresh install — likely accumulated across
        restarts. Defensive logging here so we notice next time before disk
        fills, instead of after.
        """
        if not self.chroma.available:
            return
        size = self.chroma.dir_size_bytes()
        mb = size / (1024 * 1024)
        if size >= 5 * 1024 ** 3:
            log.error("chroma dir = %.0f MB — bloat suspected; consider reset",
                      mb)
        elif size >= 500 * 1024 ** 2:
            log.warning("chroma dir = %.0f MB — watch for runaway growth", mb)

    def _recent_throttle_count(self, now: float, window_sec: float = 3600.0) -> int:
        """Count throttle events from sqlite within the trailing `window_sec`.

        Fed into `CurveContext.recent_throttle_count` so the adaptive
        policy `recent_throttle_bump` knows when the chip's been hot
        recently — letting the curve carry a small persistent boost
        instead of re-learning every event from scratch. Best-effort:
        any error path returns 0 so we never bias on bad data.
        """
        try:
            return int(self.store.count_throttle_events_since(now - window_sec))
        except (AttributeError, Exception):  # noqa: BLE001
            return 0

    def _labeled_count(self) -> int:
        """Cached labelled-Chroma-vector count.

        P2.9.6: `chroma.count_labeled()` walks the 42k-vector HNSW index
        with a where-filter on every call (~5-15 s).  Calling it from the
        hot decision path crushed tick rate to ~0.06 Hz.  Cache lives in
        `_cached_labeled_count`, refreshed every ~30 ticks together with
        the other chroma stats via `_refresh_chroma_stats`.
        """
        return self._cached_labeled_count

    def _dump_ml_state(self, prediction, features: dict[str, float]) -> None:  # type: ignore[no-untyped-def]
        # P2.9.6 — use cached labeled count (refreshed off the hot path
        # every ~30 ticks).  Direct count_labeled() walks the 42k-vector
        # HNSW index with a where-filter and took ~6s per tick.
        labeled_count = self._cached_labeled_count if self.chroma.available else -1
        snapshot = {
            "tick": self._tick_count,
            "ts": time.time(),
            "model_name": prediction.model_name,
            "horizon_sec": prediction.horizon_sec,
            "throttle_prob": prediction.throttle_prob,
            "expected_temp_c": prediction.expected_temp_c,
            "confidence": prediction.confidence,
            "reason": getattr(prediction, "reason", ""),
            "features": features,
            "calibration_ready": self.calibration_ready,
            "collectors": [c.name for c in self.collectors],
            "collector_costs_us": {c.name: c.cost().sample_us for c in self.collectors},
            "actuators": [a.name for a in self.actuators],
            "frames_in_store": self.store.count_frames(),
            "throttle_events_in_store": self.store.count_throttle_events(),
            "coverage_seconds": self.store.coverage_seconds(),
            "embedder_fitted": self.embedder.fitted,
            "chroma_available": self.chroma.available,
            # P2.9.6 phase 3: cached — refreshed every ~30 ticks in tick loop.
            "chroma_count": self._cached_chroma_count,
            "chroma_dir_bytes": self._cached_chroma_dir_bytes,
            "uptime_sec": time.time() - self._started_at,
            "neighbours": [asdict(n) for n in (prediction.neighbours or [])][:5],
            # Warm-up + throttle FSM live state (для dashboard P1-pilot)
            "labeled_count": labeled_count,
            "throttle_state": self._throttle_state,
            "throttle_episode_start_ts": self._throttle_start_ts,
            "throttle_episode_peak_temp": self._throttle_peak_temp,
            # P2.5 — surface fields the dashboard pills/tiles consume.
            "workload_label": (
                self._last_incident_snapshot.get("workload_class")
                if isinstance(self._last_incident_snapshot, dict) else None
            ),
            "session_id": self._last_session_id,
            "danger_neighbour_count": getattr(prediction, "danger_neighbour_count", 0),
            "suggested_rpm": getattr(prediction, "suggested_rpm", None),
            # Phase 4 surfacing: profile + meta-predictor state.
            "active_tuned_profile": self._profile_watcher.last_seen,
            "profile_changed_at": self._tuned_profile_changed_at or None,
            "meta_buckets": (
                len(self.predictor.bank)
                if isinstance(self.predictor, MetaPredictor) else 0
            ),
            "residual_log_count": self.residual_log.count(),
            # Live spike state.  Cockpit endpoint reads this so it can
            # surface "⚡ SPIKE 12s" without a second round-trip.
            "spike": self.spike_detector.live_state(now=time.time()),
            # Predictor refresh health (operator visibility 2026-05-13).
            # `prediction_age_sec` is now - cached_prediction_ts (i.e. how
            # stale the displayed forecast is); `refresh_skipped` is the
            # count of ticks that re-used cache because the previous async
            # refresh was still running (KNN backpressure). `refresh_ms`
            # is wall-time of the most recent completed refresh.
            "prediction_age_sec": round(
                time.time() - self._cached_prediction_ts, 2
            ) if self._cached_prediction_ts > 0 else None,
            "predict_refresh_skipped": self._predict_refresh_skipped,
            "predict_refresh_last_ms": round(self._predict_refresh_last_ms, 1),
            "predict_refresh_inflight": self._predict_task_inflight,
        }
        # P2.9.7 — surface current bucket's trust regime (prior/shrunk/confident).
        # Cockpit uses this to render dashed vs solid forecast curve.
        if isinstance(self.predictor, MetaPredictor):
            from coolstep.core.residual_meta import bucket_of
            stat = self.predictor.bank.stats.get(bucket_of(features))
            n_now = int(stat.n) if stat is not None else 0
        else:
            n_now = 0
        snapshot["trust_n"] = n_now
        snapshot["trust_mode"] = classify_trust(n_now).value
        # 2026-05-13: dump_ml_state was the dominant tick stage (~500 ms
        # under 20% CPUQuota) — visible to operator as the "тикает тикает
        # потом думает" gap.  indent=2 doubled the JSON payload size AND
        # the serialiser CPU time; compact form cuts both ~half. The file
        # is consumed exclusively by other Python processes (dashboard,
        # tooling) — none of which need it pretty.
        # Atomic write: dashboard polls this file at ~5 Hz; a torn read
        # surfaced to the operator as cockpit-tile 500s during the open-
        # truncate-write window.  Write to a sibling .tmp + os.replace
        # gives readers either the old snapshot or the new one, never a
        # half-flushed prefix. POSIX rename is atomic on the same fs.
        tmp_path = self.ml_state_path.with_name(self.ml_state_path.name + ".tmp")
        tmp_path.write_text(json.dumps(snapshot, separators=(",", ":")))
        os.replace(tmp_path, self.ml_state_path)


@click.command()
@click.option("--period", default=DEFAULT_PERIOD, type=float, help="Sample period (seconds)")
@click.option("--smoke", is_flag=True, help="Run for max_ticks and exit (smoke test)")
@click.option("--max-ticks", default=None, type=int, help="Exit after N ticks (default: forever)")
@click.option("--log-level", default="INFO")
def run_collector(period: float, smoke: bool, max_ticks: int | None, log_level: str) -> None:
    """Start the coolstep collector daemon."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    daemon = Daemon(period_sec=period)
    if smoke and max_ticks is None:
        max_ticks = 10  # 10 ticks at default period = 10s

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, daemon.request_stop)
    try:
        loop.run_until_complete(daemon.run(max_ticks=max_ticks))
    finally:
        loop.close()


if __name__ == "__main__":  # pragma: no cover
    run_collector()
