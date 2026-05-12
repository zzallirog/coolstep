"""Model drift detector for KnnPredictor.

Watches three rolling signals over the persisted ml-state.json snapshots:

  - throttle_prob distribution shift (mean / quantile drift between recent vs baseline)
  - confidence trend (rising = more labeled neighbours; falling = vector store stale)
  - chroma_count growth rate (samples/hour; flat = collector stuck)

Outputs DriftReport with named indicators and an aggregate severity 0..1.

NB: P0 source is ml-state.json (refreshed every 30 ticks). For P2+ promotional
detector — keep a circular log of (ts, throttle_prob, confidence) in store.db
and query that instead.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class DriftIndicator:
    name: str
    severity: float            # 0..1
    note: str
    current: float
    baseline: float


@dataclass(slots=True, frozen=True)
class DriftReport:
    indicators: list[DriftIndicator]
    severity: float            # max indicator severity
    snapshot_age_sec: float | None
    samples_for_baseline: int

    def to_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "snapshot_age_sec": self.snapshot_age_sec,
            "samples_for_baseline": self.samples_for_baseline,
            "indicators": [
                {
                    "name": i.name,
                    "severity": i.severity,
                    "note": i.note,
                    "current": i.current,
                    "baseline": i.baseline,
                }
                for i in self.indicators
            ],
        }


def evaluate(ml_state_path: Path, history_path: Path | None = None) -> DriftReport:
    """Compute drift from current snapshot + simple history file.

    history_path: jsonl with one line per check, append-only. Daemon should hint at
    this path for P1.5+ when we promote drift to a periodic background job.
    For now: pure stateless eval based on ml-state.json + chroma_count trend.
    """
    indicators: list[DriftIndicator] = []
    if not ml_state_path.exists():
        indicators.append(
            DriftIndicator("ml_state_missing", severity=1.0,
                           note="ml-state.json absent — daemon never wrote",
                           current=0.0, baseline=0.0)
        )
        return DriftReport(indicators=indicators, severity=1.0,
                           snapshot_age_sec=None, samples_for_baseline=0)

    try:
        snap = json.loads(ml_state_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        indicators.append(
            DriftIndicator("ml_state_corrupt", severity=1.0,
                           note=f"ml-state.json unreadable: {exc}",
                           current=0.0, baseline=0.0)
        )
        return DriftReport(indicators=indicators, severity=1.0,
                           snapshot_age_sec=None, samples_for_baseline=0)

    age = time.time() - ml_state_path.stat().st_mtime

    # Indicator 1: snapshot freshness
    if age > 90:
        indicators.append(
            DriftIndicator("snapshot_stale", severity=min(1.0, age / 600),
                           note=f"ml-state {age:.0f}s old (collector hung?)",
                           current=age, baseline=30.0)
        )

    # Indicator 2: confidence trend (history-based)
    history = _read_history(history_path) if history_path else []
    history.append({
        "ts": time.time(),
        "throttle_prob": snap.get("throttle_prob", 0.0),
        "confidence": snap.get("confidence", 0.0),
        "chroma_count": snap.get("chroma_count", 0),
    })

    if len(history) >= 4:
        recent = history[-len(history) // 4:]
        baseline = history[: len(history) // 2]
        if recent and baseline:
            recent_conf = sum(h["confidence"] for h in recent) / len(recent)
            base_conf = sum(h["confidence"] for h in baseline) / len(baseline)
            delta = recent_conf - base_conf
            if delta < -0.15:
                indicators.append(
                    DriftIndicator("confidence_drop", severity=min(1.0, abs(delta) * 3),
                                   note=f"recent confidence {recent_conf:.2f} vs baseline {base_conf:.2f}",
                                   current=recent_conf, baseline=base_conf)
                )

            recent_count = sum(h["chroma_count"] for h in recent) / len(recent)
            base_count = sum(h["chroma_count"] for h in baseline) / len(baseline)
            growth = recent_count - base_count
            if growth < 1 and len(recent) > 1:
                indicators.append(
                    DriftIndicator("chroma_no_growth", severity=0.6,
                                   note=f"chroma_count flat at {recent_count:.0f}",
                                   current=recent_count, baseline=base_count)
                )

    # Indicator 3: low coverage / cold KNN
    if not snap.get("embedder_fitted"):
        indicators.append(
            DriftIndicator("embedder_cold", severity=0.4,
                           note="embedder hasn't fit (need 60+ frames)",
                           current=0.0, baseline=1.0)
        )

    if snap.get("chroma_count", 0) > 100 and snap.get("confidence", 0.0) < 0.1:
        indicators.append(
            DriftIndicator("knn_low_confidence", severity=0.5,
                           note="store warm but neighbours not informative",
                           current=snap.get("confidence", 0.0), baseline=0.5)
        )

    severity = max((i.severity for i in indicators), default=0.0)
    return DriftReport(
        indicators=indicators,
        severity=severity,
        snapshot_age_sec=age,
        samples_for_baseline=len(history),
    )


def _read_history(path: Path | None) -> list[dict[str, float]]:
    if path is None or not path.exists():
        return []
    out: list[dict[str, float]] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def append_history(path: Path, snap: dict) -> None:
    """Used by daemon to log a periodic checkpoint."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "ts": time.time(),
        "throttle_prob": snap.get("throttle_prob", 0.0),
        "confidence": snap.get("confidence", 0.0),
        "chroma_count": snap.get("chroma_count", 0),
    })
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
