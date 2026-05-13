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
from coolstep.adapters.storage.chroma import ChromaStore
from coolstep.core import drift as drift_mod
from coolstep.core import fingerprint as fp
from coolstep.core.backfill import backfill_labels
from coolstep.core.backfill import backfill_labels as _backfill_from_events
from coolstep.core.calibration import evaluate as eval_calibration
from coolstep.core.decision import DecisionEngine
from coolstep.core.embedding import Embedder
from coolstep.core.predictor import AlwaysIdleBaseline, KnnPredictor, TrajectoryBaseline
from coolstep.core.predictor_meta import MetaPredictor
from coolstep.core.residual_meta import ResidualBank
from coolstep.core.tuned_profile_watch import ProfileWatcher
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

LOOKAHEAD_SEC = 30.0
# Operational threshold (above the per-host efficiency knee, ~5-10°C below
# thermald hard-throttle). 82°C is conservative for AMD 7940HS post-knee zone.
# Override via env COOLSTEP_HOT_THRESHOLD_C if your knee is lower/higher.
HOT_THRESHOLD_C = float(os.environ.get("COOLSTEP_HOT_THRESHOLD_C", 82.0))

DEFAULT_PERIOD = 1.0
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
        self.chroma = ChromaStore()
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
            # P2.1: hardware-safety belt on shutdown — revert anything armed
            # so SIGTERM cannot leave a fan-curve bias active forever.
            self._revert_all_armed("shutdown")
            self._persist_runtime_state()
            self.store.close()
            log.info("daemon stopped after %d ticks", self._tick_count)

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
        frame = TelemetryFrame(timestamp=time.time())
        partials = await asyncio.gather(
            *[self._sample_with_timeout(c) for c in self.collectors],
            return_exceptions=False,
        )
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

        prediction = self.predictor.predict(features, window)
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
        self._reload_mode_from_state()
        # Third defensive layer for quiet-mode: even if the predictor and
        # the decision engine both kept emitting REDUCE_NOISE, evict any
        # armed quiet bias the instant the chip crosses the eject ceiling.
        self._quiet_safety_eject(cpu_temp_c=cpu_temp_now)
        actions = self.decision.decide(
            prediction,
            calibration_ready=self.calibration_ready,
            labeled_count=self._labeled_count(),
            mode=self._mode,
            cpu_temp_c=cpu_temp_now,
        )
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
        from coolstep.core.workload_profile import resolve_profile  # local: avoids module-load cost when daemon doesn't fire actions
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

        self.ring.push(frame)
        self._labelled_window.append(frame)
        await asyncio.to_thread(self.store.write_frame, frame)
        await asyncio.to_thread(self._chroma_write, frame)
        self._throttle_fsm_tick(frame)
        # P2.1: persist on every tick so a hard crash leaves at most one
        # tick's worth of staleness in armed_actions — next start reverts
        # within `period_sec` of restart instead of forever.
        self._persist_runtime_state()

        if (self._tick_count - self._backfill_last_tick) >= self._backfill_interval_ticks:
            try:
                backfill_labels(self.store.path, self.chroma)
            except Exception as exc:  # noqa: BLE001
                log.debug("incremental backfill failed: %r", exc)
            self._backfill_last_tick = self._tick_count

        # ml-state.json drives the live dashboard predictor dot.  Every tick
        # — atomic write of ~3 KB JSON on an NVMe is <1ms.  Previously this
        # ran every 30 ticks (disk-write economy), but it meant the frontend
        # saw a `expected_temp_c` stale by up to 30 s, and the predicted dot
        # appeared "frozen" relative to live in spite of frontend correctly
        # polling 1 Hz.  The frontend frozen-lock discipline (5 s horizon)
        # is what should freeze the dot, not stale disk.
        await asyncio.to_thread(self._dump_ml_state, prediction, features)
        if self._tick_count % 30 == 0:
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

        if self._tick_count % 30 == 0 and self._tick_count > 0:
            await asyncio.to_thread(self._backfill_labels)

        if self._tick_count % 300 == 0 and self._tick_count > 0:
            await asyncio.to_thread(self._append_drift_history)

        if self._tick_count % 600 == 0 and self._tick_count > 0:
            await asyncio.to_thread(self.store.rotate)
            await asyncio.to_thread(self._chroma_size_guard)

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
            Incident, find_similar, log_incident,
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
            Incident, find_similar, log_incident,
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
                analyse_window, append_table,
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

    def _backfill_labels(self) -> None:
        """Walk the labelled-window buffer, label frames whose +30s lookahead
        has passed, push metadata back to ChromaDB, drop them from buffer."""
        if not self.chroma.available or not self._labelled_window:
            return
        now = time.time()
        kept: list[TelemetryFrame] = []
        # Build a quick lookup of cpu_temp by timestamp for the buffer
        buf_temps = []
        for f in self._labelled_window:
            t = f.cpu.temps_c.get("tctl") or f.cpu.temps_c.get("tdie") or 0.0
            buf_temps.append((f.timestamp, t))
        for f in self._labelled_window:
            if f.timestamp + LOOKAHEAD_SEC > now:
                kept.append(f)
                continue
            future_temps = [t for ts, t in buf_temps if f.timestamp <= ts <= f.timestamp + LOOKAHEAD_SEC]
            peak = max(future_temps) if future_temps else 0.0
            label = LABEL_HOT if peak >= HOT_THRESHOLD_C else LABEL_COOL
            self.chroma.update_metadata(
                f.timestamp,
                {"was_hot_in_30s": label, "peak_temp_after": float(peak)},
            )
        # Trim buffer to recent frames only (≤ 2 × LOOKAHEAD_SEC ago)
        cutoff = now - 2 * LOOKAHEAD_SEC
        self._labelled_window = [f for f in kept if f.timestamp >= cutoff]

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
        """Live count of labelled Chroma vectors (was_hot_in_30s != UNKNOWN).

        Used by `_tick` to gate P2.2 RAMP_COOLING auto-fire on KNN coverage.
        Best-effort — every error path returns 0 (i.e. "cold index, hold the
        bias"), never raises. ChromaStore.count_labeled already swallows its
        own exceptions; the try/except here is the belt against future
        backends that aren't as forgiving.
        """
        if not getattr(self, "chroma", None) or not self.chroma.available:
            return 0
        try:
            return int(self.chroma.count_labeled())
        except Exception:  # noqa: BLE001
            return 0

    def _dump_ml_state(self, prediction, features: dict[str, float]) -> None:  # type: ignore[no-untyped-def]
        # Warm-up indicator: counted labelled (was_hot_in_30s != UNKNOWN)
        # vectors in Chroma. Predictor needs ≥ max(3, top_k//4) before it
        # emits non-zero confidence (predictor-audit risk #4). Without this
        # field, dashboard cannot show "N/5 labelled neighbours collected".
        labeled_count = 0
        try:
            if self.chroma.available:
                labeled_count = int(self.chroma.count_labeled())
        except (AttributeError, Exception):  # noqa: BLE001
            labeled_count = -1  # not exposed by store backend
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
            "chroma_count": self.chroma.count(),
            "chroma_dir_bytes": self.chroma.dir_size_bytes(),
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
        }
        self.ml_state_path.write_text(json.dumps(snapshot, indent=2))


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
