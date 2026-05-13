"""Spike detector — turn sustained predictor failures into labelled training data.

Operator-observed pattern: a fresh workload (speedtest, steam launch, zen
fetch) hits the chip with a step input the predictor hasn't seen before.
For the first ~5 ticks the predictor overshoots by 2-5°C, then catches up
and stabilises within one or two horizon windows.  Those 5 ticks are
exactly the moments worth saving — they're the boundary between "what we
modeled" and "what surprised us", and they're labelled by the residual
itself ("we predicted T, it became T+Δ").

The detector watches the residual stream and emits a SpikeRecord at the
end of each transient.  Daemon takes that record and:

  1. Logs an Incident(kind="predictor_spike") via the existing journal
     so it shows up alongside quiet-eject / throttle-close in the
     incidents tile and feeds the multi-angle similarity search.
  2. When ChromaDB is alive (currently masked off, see TODO «ChromaDB
     SEGV») the same record becomes a weighted training sample for the
     KNN store — weight = severity (max |residual|) × duration.

State machine (intentionally simple, no PID, no Kalman — observable
behaviour first, math second):

    IDLE                  |res| ≥ entry_thresh for entry_n ticks → ACTIVE
    ACTIVE                |res| < exit_thresh  for exit_n ticks  → IDLE+emit (calmed)
    ACTIVE                res ≤ −neg_thresh    for neg_n ticks   → IDLE+emit (margin)

The third transition (P2.9.1) closes spikes that opened on a real
residual but then refused to calm because the predictor *systematically*
over-predicts for this workload — signed residual stays negative for a
long stretch (cooling outperforms historical envelope).  Without it, a
single step can keep the spike open indefinitely (operator observed
kitty spike open 34 min, signed residual stable near −20°C).  Closing
with `closure_reason="predictor_margin_exceeded"` distinguishes
"predictor right, cooling caught up" from "real interactive burst
calmed" in the journal.

Defaults (5° / 2° / 2-tick / 3-tick) chosen from the operator's live
speedtest observation: a real new-workload spike clears 5°C in 1-2 ticks
and never returns under 2°C without the workload genuinely calming.
Negative-exit defaults (−5° / 10-tick) treat 10 s of sustained
over-prediction as "predictor conservative, not surprised".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SpikeState:
    """Live state — re-created from scratch on each spike closure."""
    active: bool = False
    started_at: float | None = None
    ts_high_streak: int = 0
    ts_low_streak: int = 0
    ts_neg_streak: int = 0
    max_abs_residual: float = 0.0
    peak_temp_c: float = 0.0
    workload_label: str | None = None
    started_features: dict[str, float] = field(default_factory=dict)
    n_validations: int = 0


@dataclass(slots=True)
class SpikeRecord:
    """Closed spike — handed off to the incident journal + (future) KNN
    backfill.  All fields are deliberately ledger-safe: floats only,
    no objects, no references back into the detector.

    `closure_reason` distinguishes how the spike ended:
      - "calmed": |residual| dropped below exit_thresh_c for exit_n ticks
        (real interactive burst settled — actuators or workload calmed)
      - "predictor_margin_exceeded": residual stayed signed-negative
        beyond -negative_exit_thresh_c for negative_exit_n ticks
        (predictor systematically over-predicted; cooling outperformed
        historical envelope — safe direction, not a real spike)
    """
    started_at: float
    ended_at: float
    duration_s: float
    max_abs_residual: float
    peak_temp_c: float
    workload_label: str | None
    n_validations: int
    started_features: dict[str, float]
    predictor_model: str
    closure_reason: str = "calmed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": float(self.started_at),
            "ended_at": float(self.ended_at),
            "duration_s": float(self.duration_s),
            "max_abs_residual": float(self.max_abs_residual),
            "peak_temp_c": float(self.peak_temp_c),
            "workload_label": self.workload_label,
            "n_validations": int(self.n_validations),
            "started_features": dict(self.started_features),
            "predictor_model": str(self.predictor_model),
            "closure_reason": str(self.closure_reason),
        }


class SpikeDetector:
    """Stateful residual-stream watcher.  Push every validated residual
    via :meth:`update`; receive a SpikeRecord at the end of each spike.

    The detector keeps no history beyond the current state — closed
    spikes are the caller's responsibility (write to ledger / incident).
    """

    def __init__(
        self,
        *,
        entry_thresh_c: float = 5.0,
        exit_thresh_c: float = 2.0,
        entry_n: int = 2,
        exit_n: int = 3,
        negative_exit_thresh_c: float = 5.0,
        negative_exit_n: int = 10,
    ) -> None:
        if entry_thresh_c <= exit_thresh_c:
            raise ValueError("entry threshold must exceed exit threshold")
        if entry_n < 1 or exit_n < 1 or negative_exit_n < 1:
            raise ValueError("debounce counts must be ≥ 1")
        if negative_exit_thresh_c <= 0:
            raise ValueError("negative exit threshold must be positive (compared against −value)")
        self.entry_thresh_c = float(entry_thresh_c)
        self.exit_thresh_c = float(exit_thresh_c)
        self.entry_n = int(entry_n)
        self.exit_n = int(exit_n)
        self.negative_exit_thresh_c = float(negative_exit_thresh_c)
        self.negative_exit_n = int(negative_exit_n)
        self.state = SpikeState()

    def update(
        self,
        *,
        ts: float,
        residual_c: float,
        peak_temp_c: float,
        workload_label: str | None,
        features: dict[str, float],
        predictor_model: str,
    ) -> SpikeRecord | None:
        """Feed one validated residual.  Returns SpikeRecord on closure,
        None on every other tick (entry-pending, in-spike, idle)."""
        abs_res = abs(float(residual_c))
        st = self.state
        if not st.active:
            if abs_res >= self.entry_thresh_c:
                st.ts_high_streak += 1
            else:
                st.ts_high_streak = 0
            if st.ts_high_streak >= self.entry_n:
                # Open a fresh spike.  Snapshot the entry context so the
                # closure record can report what conditions opened it.
                st.active = True
                st.started_at = float(ts)
                st.max_abs_residual = abs_res
                st.peak_temp_c = float(peak_temp_c)
                st.workload_label = workload_label
                st.started_features = {
                    k: float(v) for k, v in features.items()
                    if isinstance(v, (int, float))
                }
                # n_validations seeds with the high streak so the
                # closure count includes the ticks that triggered entry.
                st.n_validations = st.ts_high_streak
                st.ts_low_streak = 0
            return None

        # ACTIVE path — track maxima, count, watch for exits.
        signed_res = float(residual_c)
        st.n_validations += 1
        if abs_res > st.max_abs_residual:
            st.max_abs_residual = abs_res
        if peak_temp_c > st.peak_temp_c:
            st.peak_temp_c = float(peak_temp_c)
        # Calmed-exit streak: |residual| dropped below exit threshold
        if abs_res < self.exit_thresh_c:
            st.ts_low_streak += 1
        else:
            st.ts_low_streak = 0
        # Predictor-margin-exceeded streak (P2.9.1): signed-negative
        # residual deeper than −negative_exit_thresh_c.  Real interactive
        # bursts oscillate sign at decay; chronic over-prediction stays
        # one-sided.  Reset on any tick that isn't deeply negative.
        if signed_res <= -self.negative_exit_thresh_c:
            st.ts_neg_streak += 1
        else:
            st.ts_neg_streak = 0
        closure_reason: str | None = None
        if st.ts_low_streak >= self.exit_n:
            closure_reason = "calmed"
        elif st.ts_neg_streak >= self.negative_exit_n:
            closure_reason = "predictor_margin_exceeded"
        if closure_reason is not None:
            assert st.started_at is not None  # invariant: active ⇒ started_at set
            record = SpikeRecord(
                started_at=st.started_at,
                ended_at=float(ts),
                duration_s=float(ts) - st.started_at,
                max_abs_residual=st.max_abs_residual,
                peak_temp_c=st.peak_temp_c,
                workload_label=st.workload_label,
                n_validations=st.n_validations,
                started_features=dict(st.started_features),
                predictor_model=str(predictor_model),
                closure_reason=closure_reason,
            )
            self.state = SpikeState()
            return record
        return None

    def live_state(self, *, now: float | None = None) -> dict[str, Any]:
        """Dashboard-friendly snapshot.  Shape kept flat so the cockpit
        endpoint can splice it into its aggregator JSON without
        re-shaping."""
        if now is None:
            now = time.time()
        st = self.state
        return {
            "active": bool(st.active),
            "started_at": st.started_at,
            "duration_s": (now - st.started_at) if (st.active and st.started_at is not None) else None,
            "max_abs_residual": st.max_abs_residual if st.active else None,
            "peak_temp_c": st.peak_temp_c if st.active else None,
            "workload_label": st.workload_label if st.active else None,
            "n_validations": st.n_validations if st.active else 0,
            "thresholds": {
                "entry_c": self.entry_thresh_c,
                "exit_c": self.exit_thresh_c,
                "entry_n": self.entry_n,
                "exit_n": self.exit_n,
                "negative_exit_c": self.negative_exit_thresh_c,
                "negative_exit_n": self.negative_exit_n,
            },
        }
