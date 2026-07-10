"""Decision engine: prediction + calibration → list[Action].

Calibration gate `ready` is a boolean owned by the daemon (driven by
calibration.py). When NOT ready — DecisionEngine still emits *intent*
Actions (NOTIFY_USER), so the dashboard can show what the system would do.
The daemon's dual-pass router journals every action via readonly_log and
routes NOTIFY_USER to notify_send when present (desktop notification —
informational, not a hardware write).

When ready — Actions are still gated by Predictor.confidence and
throttle_prob thresholds.

P2.2 (KNN auto-fire gating) — additional gates protect the soft bias path
(`RAMP_COOLING`) from firing while the KNN index is cold or the model is
uncertain. Three knobs:

* `Thresholds.min_arm_labeled_count` — minimum labelled neighbours in
  Chroma before bias may fire (default 5). Daemon passes the live count
  into `decide(..., labeled_count=...)`.
* `Thresholds.min_arm_confidence` — gentler confidence floor for the bias
  path only (default 0.5; harder actions keep using `min_confidence`).
* `Thresholds.rearm_gap_s` — mirror of `daemon.REARM_GAP_SEC`; the daemon
  enforces the gap (this field is informational, used by tests and the
  dashboard render).
* `Thresholds.ramp_cooling_temp_margin_c` +
  `Thresholds.ramp_cooling_cooling_slope_c_per_sec` — block anticipatory
  fan ramp when the chip is already cooling and the forecast is not above
  the current temperature by a meaningful margin.

Env escape hatches (read by `DecisionEngine` itself, single source of
truth — daemon should not duplicate these reads):

* `COOLSTEP_FORCE_FIRE_RAMP=1` — TEST-ONLY. Bypasses the labeled-count
  gate, the confidence gate, AND the calibration gate for `RAMP_COOLING`.
  Used by `bench/stress.sh` to exercise the hot path without natural load.
* `COOLSTEP_ACTUATOR_ALLOW_PRE_CALIBRATION=1` — allow `RAMP_COOLING` to
  fire while `calibration_ready=False`. Other verbs stay blocked.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from coolstep.core.predictor import Prediction
from coolstep.core.schema import Action, ActionVerb

log = logging.getLogger(__name__)

# Append-only JSONL of every `DecisionEngine.decide()` call. Lives next to
# `actuator-journal.jsonl` in `data/` but is intentionally a separate file:
#
#   * actuator-journal records APPLY/REVERT side effects (one entry per
#     actuator firing). Many decisions yield zero actions — those would be
#     invisible in the actuator journal but are exactly what offline drift
#     studies need.
#   * decisions.jsonl records EVERY decide() call, including the empty
#     ones, with prediction + thresholds + calibration context. That makes
#     it a stable timeline for threshold tuning and post-mortem replay.
#
# The two logs intentionally duplicate the rotation pattern (3 generations,
# size-based) — they're young enough that extracting a shared helper would
# require breaking _base.py out of the actuators package or adding a new
# core helper. Both options carry more risk than the ~15 lines of duplicated
# rotate logic. Revisit when a third journal needs the same scheme. The
# duplication is marked inline (see `_rotate_decisions_log`) so future
# refactor finds both sites.
DECISIONS_FILE = "decisions.jsonl"
# 5 MB — decisions records are denser than journal entries (prediction +
# thresholds + actions list per line), so allow more bytes before rotation
# than `actuator-journal.jsonl`'s 1 MB default.
MAX_DECISIONS_BYTES: int = int(
    os.environ.get("COOLSTEP_DECISIONS_MAX_BYTES", "5000000")
)


def _ramp_intensity_for(throttle_prob: float) -> float:
    """Map throttle_prob → intensity_pct in [5.0, 20.0].

    Linear formula:  intensity = (prob - 0.5) * 40
      prob=0.625 → 5.0  (floor)
      prob=0.75  → 10.0
      prob=0.875 → 15.0
      prob=1.0   → 20.0 (ceiling)

    Values below the floor are clamped to 5.0; actuator also clamps
    defensively, but we emit honest values here.
    """
    return max(5.0, min(20.0, (throttle_prob - 0.5) * 40.0))


def _decisions_log_path() -> Path:
    """Resolve `data/decisions.jsonl` using the same `COOLSTEP_HOME` knob the
    daemon honours (see `daemon._coolstep_home`). Single source of truth — no
    new env var introduced.
    """
    home = Path(os.environ.get("COOLSTEP_HOME", str(Path.home() / "coolstep" / "data")))
    return home / DECISIONS_FILE


def _rotate_decisions_log(path: Path) -> None:
    """Rotate decisions log: .1→.2, active→.1. Mirrors
    `actuators._base._rotate_journal` — duplicated intentionally, see top-of-
    file comment for the rationale.
    """
    gen2 = Path(str(path) + ".2")
    gen1 = Path(str(path) + ".1")
    try:
        if gen1.exists():
            os.rename(gen1, gen2)
        os.rename(path, gen1)
    except OSError as exc:
        log.debug("decisions log rotation failed: %r", exc)


def _append_decision_log(
    prediction: Prediction,
    actions: list[Action],
    calibration_ready: bool,
    labeled_count: int,
    thresholds: Thresholds,
) -> None:
    """Append one JSON line capturing this decide() call.

    Best-effort: any OSError → debug log, never raises. Skipped entirely when
    `COOLSTEP_DECISIONS_LOG_DISABLED` is truthy — tests opt out of writing to
    the user's real data dir.

    Schema (see module docstring + spec):
        ts, prediction{throttle_prob, confidence, expected_temp_c, reason,
        model_name}, calibration_ready, labeled_count,
        actions[{verb, params, expires_at}], thresholds{...}
    """
    if _env_truthy("COOLSTEP_DECISIONS_LOG_DISABLED"):
        return
    try:
        path = _decisions_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= MAX_DECISIONS_BYTES:
            _rotate_decisions_log(path)
        record = {
            "ts": time.time(),
            "prediction": {
                "throttle_prob": float(prediction.throttle_prob),
                "confidence": float(prediction.confidence),
                "expected_temp_c": (
                    float(prediction.expected_temp_c)
                    if prediction.expected_temp_c is not None
                    else None
                ),
                "reason": str(getattr(prediction, "reason", "") or ""),
                "model_name": str(prediction.model_name),
            },
            "calibration_ready": bool(calibration_ready),
            "labeled_count": int(labeled_count),
            "actions": [
                {
                    "verb": a.verb.value,
                    "params": dict(a.params),
                    "expires_at": float(a.expires_at),
                }
                for a in actions
            ],
            "thresholds": {
                "notify_prob": float(thresholds.notify_prob),
                "soft_action_prob": float(thresholds.soft_action_prob),
                "hard_action_prob": float(thresholds.hard_action_prob),
                "min_arm_confidence": float(thresholds.min_arm_confidence),
                "min_arm_labeled_count": int(thresholds.min_arm_labeled_count),
                "ramp_cooling_temp_margin_c": float(
                    thresholds.ramp_cooling_temp_margin_c
                ),
                "ramp_cooling_cooling_slope_c_per_sec": float(
                    thresholds.ramp_cooling_cooling_slope_c_per_sec
                ),
            },
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.debug("decisions log append failed: %r", exc)


def _env_truthy(name: str) -> bool:
    """Return True when env var `name` is set to a truthy value.

    Treats "0", "false", "no", "off" (case-insensitive) and unset as falsy.
    Anything else that is non-empty is truthy. Keeps the parsing here so the
    daemon doesn't duplicate it.
    """
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


@dataclass(slots=True)
class Thresholds:
    notify_prob: float = 0.4    # надёжно notify
    soft_action_prob: float = 0.7   # ramp_cooling / cap_boost
    hard_action_prob: float = 0.9   # shift_power_envelope / defer
    min_confidence: float = 0.6
    # P2.2 KNN auto-fire gates (soft bias path only):
    min_arm_confidence: float = 0.5   # relaxed floor for RAMP_COOLING
    min_arm_labeled_count: int = 5    # min labelled neighbours in Chroma
    rearm_gap_s: float = 15.0         # mirror of daemon.REARM_GAP_SEC
    # If the live short slope already says the chip is cooling, do not
    # pre-spin fans unless the forecast still rises above current temp by
    # this margin. Prevents early duplicate response on a recovery edge.
    ramp_cooling_temp_margin_c: float = 0.5
    ramp_cooling_cooling_slope_c_per_sec: float = -0.05

    # P2.3 — quiet-mode gates (only relevant when DecisionEngine.decide(mode='quiet')).
    # REDUCE_NOISE fires when the predictor is CONFIDENTLY calm AND the
    # current frame is well below the thermal knee. The two-sided gate
    # protects against silently letting the chip drift up while quiet-mode
    # holds the fan curve down.
    quiet_calm_prob_max: float = 0.15      # throttle_prob ceiling for quiet to fire
    quiet_calm_temp_max_c: float = 75.0    # current Tctl must be below this
    quiet_eject_temp_c: float = 80.0       # >= this → daemon evicts quiet bias
    quiet_negative_intensity_pct: float = 10.0   # how much to subtract at knee
    quiet_min_confidence: float = 0.6      # predictor confidence floor

    # P2.5 — Conservative-mode floor. When KNN confidence is below this
    # AND there's at least some heat hint (throttle_prob > 0.2), the
    # downstream curve pipeline shifts to a wider trigger threshold and
    # a slightly larger preload amplitude. Trades silence for safety —
    # the model doesn't know this scene, so we'd rather pre-spin fans
    # than risk a thermo-shock. Disabled by setting floor=0 (effectively
    # off — no realistic confidence is below 0).
    conservative_floor: float = 0.45
    conservative_prob_hint: float = 0.2


class DecisionEngine:
    def __init__(self, thresholds: Thresholds | None = None) -> None:
        self.thresholds = thresholds or Thresholds()

    def decide(
        self,
        prediction: Prediction,
        calibration_ready: bool,
        now: float | None = None,
        labeled_count: int = 0,
        mode: str = "cool",
        cpu_temp_c: float | None = None,
        cpu_temp_slope_c_per_sec: float | None = None,
    ) -> list[Action]:
        """Map prediction → list[Action].

        Args:
            mode: operational mode. ``"cool"`` (default) keeps the original
                anticipatory-cooling policy. ``"quiet"`` flips the policy:
                while the predictor is confidently calm AND ``cpu_temp_c``
                is well below the thermal knee, emit ``REDUCE_NOISE`` so the
                actuator can subtract from the fan curve. ``"off"`` blocks
                every actionable verb — only ``NOTIFY_USER`` still flows.
            cpu_temp_c: current Tctl in °C (live, not predicted). Required
                for the quiet-mode safety belt — quiet only fires when the
                chip is *actually* below ``quiet_calm_temp_max_c``. ``None``
                means «unknown», which disables quiet-mode emission for safety.
            cpu_temp_slope_c_per_sec: short live Tctl slope. When this is
                already cooling and ``expected_temp_c`` is not meaningfully
                above the current temperature, ``RAMP_COOLING`` is suppressed
                so an earlier actuator response does not reinforce itself.
        """
        ts = now if now is not None else time.time()
        actions: list[Action] = []

        force_fire = _env_truthy("COOLSTEP_FORCE_FIRE_RAMP")
        allow_pre_cal = _env_truthy("COOLSTEP_ACTUATOR_ALLOW_PRE_CALIBRATION")
        mode = (mode or "cool").lower()
        if mode not in {"cool", "quiet", "off"}:
            mode = "cool"

        # Hard floor: predictor confidence below BOTH the global minimum and
        # the gentler arm floor kills everything except the test-only
        # force-fire path. If confidence is between min_arm_confidence and
        # min_confidence we keep going — RAMP_COOLING may still fire below.
        if (
            prediction.confidence < self.thresholds.min_arm_confidence
            and not force_fire
        ):
            # Still log: the module contract is «decisions.jsonl records
            # EVERY decide() call». Skipping the low-confidence region made
            # the log blind exactly where min_arm_confidence would be tuned.
            _append_decision_log(
                prediction, actions, calibration_ready, labeled_count,
                self.thresholds,
            )
            return actions

        # --- Hard verb: shift_power_envelope ---
        # Keeps the strict `min_confidence` + `calibration_ready` gating.
        # `mode == "off"` is a global kill for actionable verbs — notify
        # still flows so the user keeps awareness of what *would* fire.
        if (
            mode != "off"
            and prediction.throttle_prob >= self.thresholds.hard_action_prob
            and prediction.confidence >= self.thresholds.min_confidence
            and calibration_ready
        ):
            actions.append(
                Action(
                    verb=ActionVerb.SHIFT_POWER_ENVELOPE,
                    params={"direction": "balance_power", "horizon_sec": prediction.horizon_sec},
                    expires_at=ts + prediction.horizon_sec * 2,
                )
            )

        # --- Soft bias path: RAMP_COOLING + CAP_BOOST ---
        # CAP_BOOST keeps the strict gate; RAMP_COOLING gets the relaxed
        # confidence floor + labeled_count precondition + env hatches.
        # Quiet mode still allows RAMP_COOLING — that's the safety override
        # path (quiet bias was wrong, we're heading toward throttle, ramp).
        if (
            mode != "off"
            and prediction.throttle_prob >= self.thresholds.soft_action_prob
            and prediction.confidence >= self.thresholds.min_confidence
            and calibration_ready
        ):
            actions.append(
                Action(
                    verb=ActionVerb.CAP_BOOST,
                    params={"severity": "mild", "duration_sec": prediction.horizon_sec},
                    expires_at=ts + prediction.horizon_sec,
                )
            )

        ramp_allowed = (
            mode != "off"
            and self._ramp_cooling_allowed(
                prediction=prediction,
                calibration_ready=calibration_ready,
                labeled_count=labeled_count,
                force_fire=force_fire,
                allow_pre_cal=allow_pre_cal,
                cpu_temp_c=cpu_temp_c,
                cpu_temp_slope_c_per_sec=cpu_temp_slope_c_per_sec,
            )
        )
        if ramp_allowed:
            actions.append(
                Action(
                    verb=ActionVerb.RAMP_COOLING,
                    params={
                        "intensity_pct": _ramp_intensity_for(prediction.throttle_prob),
                        "duration_sec": prediction.horizon_sec * 1.5,
                    },
                    expires_at=ts + prediction.horizon_sec * 1.5,
                )
            )

        # --- Quiet-mode bias path: REDUCE_NOISE ---
        # Only emitted in quiet mode. Subtractive bias on the fan curve to
        # lower noise during confidently calm windows. The gate is BOTH
        # sides: predictor must say calm AND current Tctl must be safely
        # below the thermal knee. The actuator-side safety belt (Tctl
        # exceeding `quiet_eject_temp_c`) is a third line of defense.
        quiet_allowed = self._reduce_noise_allowed(
            mode=mode,
            prediction=prediction,
            cpu_temp_c=cpu_temp_c,
            calibration_ready=calibration_ready,
            labeled_count=labeled_count,
            allow_pre_cal=allow_pre_cal,
        )
        if quiet_allowed:
            actions.append(
                Action(
                    verb=ActionVerb.REDUCE_NOISE,
                    params={
                        "intensity_pct": float(self.thresholds.quiet_negative_intensity_pct),
                        "duration_sec": prediction.horizon_sec * 1.5,
                        "eject_temp_c": float(self.thresholds.quiet_eject_temp_c),
                    },
                    expires_at=ts + prediction.horizon_sec * 1.5,
                )
            )

        # --- Notify: independent informational track ---
        if (
            prediction.throttle_prob >= self.thresholds.notify_prob
            and prediction.confidence >= self.thresholds.min_confidence
        ):
            actions.append(
                Action(
                    verb=ActionVerb.NOTIFY_USER,
                    params={
                        "message": (
                            f"Predicting thermal pressure (p={prediction.throttle_prob:.0%}) "
                            f"in next {prediction.horizon_sec:.0f}s"
                        ),
                        "urgency": "low" if prediction.throttle_prob < 0.7 else "normal",
                    },
                    expires_at=ts + prediction.horizon_sec,
                )
            )
        _append_decision_log(
            prediction,
            actions,
            calibration_ready,
            labeled_count,
            self.thresholds,
        )
        return actions

    def _ramp_cooling_allowed(
        self,
        prediction: Prediction,
        calibration_ready: bool,
        labeled_count: int,
        force_fire: bool,
        allow_pre_cal: bool,
        cpu_temp_c: float | None,
        cpu_temp_slope_c_per_sec: float | None,
    ) -> bool:
        """Resolve whether `RAMP_COOLING` may be emitted this tick.

        The bias is gentle — actuator-side it's a small fan-curve nudge —
        so we trade strict confidence for a coverage precondition (enough
        labelled neighbours in the KNN index).
        """
        if force_fire:
            # Test-only: bypass every gate INCLUDING throttle_prob threshold,
            # so bench/stress.sh can drive the hot path on demand.
            return True
        if prediction.throttle_prob < self.thresholds.soft_action_prob:
            return False
        if prediction.confidence < self.thresholds.min_arm_confidence:
            return False
        if not calibration_ready and not allow_pre_cal:
            return False
        if labeled_count < self.thresholds.min_arm_labeled_count:
            return False
        already_cooling_to_forecast = (
            cpu_temp_c is not None
            and prediction.expected_temp_c is not None
            and cpu_temp_slope_c_per_sec is not None
            and cpu_temp_slope_c_per_sec <= (
                self.thresholds.ramp_cooling_cooling_slope_c_per_sec
            )
            and prediction.expected_temp_c <= (
                cpu_temp_c + self.thresholds.ramp_cooling_temp_margin_c
            )
        )
        return not already_cooling_to_forecast

    def _reduce_noise_allowed(
        self,
        mode: str,
        prediction: Prediction,
        cpu_temp_c: float | None,
        calibration_ready: bool,
        labeled_count: int,
        allow_pre_cal: bool,
    ) -> bool:
        """Resolve whether `REDUCE_NOISE` may be emitted this tick.

        Quiet bias is inverse to cooling bias — it *lowers* fan speed in
        the knee band — so the gate is stricter than RAMP_COOLING:

          • mode must be exactly «quiet» (cool/off never emit)
          • predictor confidence must clear `quiet_min_confidence`
          • throttle_prob must be at or below `quiet_calm_prob_max`
          • current Tctl (live, not predicted) must be below
            `quiet_calm_temp_max_c` — unknown temperature = no fire
          • KNN coverage gate same as RAMP_COOLING: enough labelled
            neighbours so the calm classification is trustworthy
          • calibration gate honoured unless the user has explicitly opted
            into pre-calibration via the env hatch

        The actuator carries its own kill-switch on cpu_temp_c >=
        `quiet_eject_temp_c` (passed via Action.params); that's the third
        line of defense if the predictor's «calm» reading lags reality.
        """
        if mode != "quiet":
            return False
        if cpu_temp_c is None:
            # Unknown temperature → refuse to bias. Better noisy than
            # silently sliding into thermal pressure with no feedback.
            return False
        if cpu_temp_c >= self.thresholds.quiet_calm_temp_max_c:
            return False
        if prediction.throttle_prob > self.thresholds.quiet_calm_prob_max:
            return False
        if prediction.confidence < self.thresholds.quiet_min_confidence:
            return False
        if not calibration_ready and not allow_pre_cal:
            return False
        return labeled_count >= self.thresholds.min_arm_labeled_count
