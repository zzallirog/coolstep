"""Residual log — append-only JSONL of validated predictions.

A "residual" is observed when a prediction made at t=T0 reaches its horizon
at t=T0+horizon_sec: at that moment we know both what was predicted and
what actually happened, so we can record the error.  Residuals are the
substrate the meta-predictor (Phase 2) learns from.

Schema (one JSON object per line, NDJSON):

    {
      "ts": 1778600000.123,            # validation moment (when written)
      "predicted_at": 1778599995.0,    # when the prediction was made
      "horizon_sec": 5.0,
      "predicted_temp_c": 77.4,
      "actual_temp_c": 57.3,
      "residual_c": -20.1,             # actual - predicted (signed)
      "model_name": "trajectory_baseline",
      "bucket_key": [1, 2, 1],         # set later by meta-predictor; null pre-Phase-2
      "profile": "workstation-latency-quiet",
      "features": {                    # snapshot of the features the prediction was keyed on
        "cpu_temp_max": 77.6,
        "cpu_temp_slope_per_sec": 0.02,
        "cpu_load_avg": 12.0,
        "cpu_load_slope_per_sec": -3.5,
        "heat_soak_index": 0.0
      }
    }

Why append-only JSONL and not sqlite: matches the existing pattern of
`incidents.jsonl`, `decisions.jsonl`, `actuator-journal.jsonl` — one shape
of operational log, one rotation policy, one set of mental models.  See
ADR-016 in docs/stack-decisions.md.

Rotation: when the live file exceeds `max_bytes`, it is renamed with a
`.1` suffix (and `.1` → `.2`, and `.2` is dropped).  Three generations
covers ~3·max_bytes worth of history.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_MAX_BYTES = 10 * 1024 * 1024   # 10 MB per generation; ~3 days of 5s ticks
DEFAULT_GENERATIONS = 3                 # .1 and .2 archives kept; .3 dropped


@dataclass(frozen=True)
class ResidualRecord:
    """One validated prediction."""

    ts: float
    predicted_at: float
    horizon_sec: float
    predicted_temp_c: float
    actual_temp_c: float
    residual_c: float
    model_name: str
    profile: str | None = None
    bucket_key: tuple[int, ...] | None = None
    features: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_pair(
        cls,
        ts: float,
        predicted_at: float,
        horizon_sec: float,
        predicted_temp_c: float,
        actual_temp_c: float,
        model_name: str,
        *,
        profile: str | None = None,
        bucket_key: tuple[int, ...] | None = None,
        features: dict[str, float] | None = None,
    ) -> ResidualRecord:
        return cls(
            ts=ts,
            predicted_at=predicted_at,
            horizon_sec=horizon_sec,
            predicted_temp_c=predicted_temp_c,
            actual_temp_c=actual_temp_c,
            residual_c=actual_temp_c - predicted_temp_c,
            model_name=model_name,
            profile=profile,
            bucket_key=bucket_key,
            features=features or {},
        )

    def to_jsonable(self) -> dict[str, object]:
        d = asdict(self)
        if d["bucket_key"] is not None:
            d["bucket_key"] = list(d["bucket_key"])
        return d


class ResidualLog:
    """Append-only JSONL with size-based rotation.  Single-writer assumed."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        generations: int = DEFAULT_GENERATIONS,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.generations = generations
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: ResidualRecord) -> None:
        line = json.dumps(record.to_jsonable(), separators=(",", ":")) + "\n"
        # Rotate BEFORE writing if the new line would push us past the cap.
        # Otherwise we'd have an oversized file for one cycle, which the
        # dashboard tail would then have to truncate-read.
        try:
            current_size = self.path.stat().st_size
        except FileNotFoundError:
            current_size = 0
        if current_size + len(line) > self.max_bytes:
            self._rotate()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)

    def _rotate(self) -> None:
        """Cascade .N → .N+1; drop the oldest."""
        oldest = self.path.with_suffix(self.path.suffix + f".{self.generations - 1}")
        if oldest.exists():
            oldest.unlink()
        for i in range(self.generations - 2, -1, -1):
            src = self.path if i == 0 else self.path.with_suffix(self.path.suffix + f".{i}")
            dst = self.path.with_suffix(self.path.suffix + f".{i + 1}")
            if src.exists():
                src.rename(dst)

    def tail(self, n: int) -> list[ResidualRecord]:
        """Return the last `n` records from the live file.

        Streams the file with `deque(maxlen=n)` so only the last `n` raw
        lines stay in memory; json.loads parses those `n`, not the whole
        file. At ~50k records (6.7MB) this cuts `/api/predictor-cockpit`
        wall time from ~6s under quota to ~50ms (the visible "freeze every
        5 polls" the operator reported 2026-05-13).
        """
        if n <= 0 or not self.path.exists():
            return []
        try:
            with open(self.path, encoding="utf-8") as f:
                tail_lines = deque(f, maxlen=n)
        except OSError:
            return []
        records: list[ResidualRecord] = []
        for line in tail_lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            bk = obj.get("bucket_key")
            if bk is not None:
                bk = tuple(int(x) for x in bk)
            try:
                records.append(ResidualRecord(
                    ts=float(obj["ts"]),
                    predicted_at=float(obj["predicted_at"]),
                    horizon_sec=float(obj["horizon_sec"]),
                    predicted_temp_c=float(obj["predicted_temp_c"]),
                    actual_temp_c=float(obj["actual_temp_c"]),
                    residual_c=float(obj["residual_c"]),
                    model_name=str(obj["model_name"]),
                    profile=obj.get("profile"),
                    # default_factory=dict on the dataclass only fires when
                    # the keyword is omitted; an explicit None here would
                    # land verbatim and any consumer doing rec.features.items()
                    # would AttributeError. Coerce None / missing to {}.
                    features=dict(obj.get("features") or {}),
                    bucket_key=bk,
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return records

    def iter_all(self) -> Iterator[ResidualRecord]:
        """Iterate every record, oldest archive first → live file last.
        Used by `ResidualBank.from_log()` at startup to rebuild state."""
        for i in range(self.generations - 1, 0, -1):
            archived = self.path.with_suffix(self.path.suffix + f".{i}")
            if archived.exists():
                yield from self._iter_file(archived)
        if self.path.exists():
            yield from self._iter_file(self.path)

    @staticmethod
    def _iter_file(path: Path) -> Iterator[ResidualRecord]:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    # Truncated final line after a crash.  Skip.  We don't
                    # warn — operational logs intentionally tolerate this.
                    continue
                bk = obj.get("bucket_key")
                if bk is not None:
                    bk = tuple(int(x) for x in bk)
                yield ResidualRecord(
                    ts=float(obj["ts"]),
                    predicted_at=float(obj["predicted_at"]),
                    horizon_sec=float(obj["horizon_sec"]),
                    predicted_temp_c=float(obj["predicted_temp_c"]),
                    actual_temp_c=float(obj["actual_temp_c"]),
                    residual_c=float(obj["residual_c"]),
                    model_name=str(obj["model_name"]),
                    profile=obj.get("profile"),
                    bucket_key=bk,
                    features=dict(obj.get("features", {})),
                )

    def count(self) -> int:
        """Cheap line count of live file only (ignores archives).  Used by
        dashboard for "N residuals collected" pill."""
        if not self.path.exists():
            return 0
        with open(self.path, "rb") as f:
            # Subtract 1 if last byte is not newline (incomplete trailing line)
            return sum(1 for _ in f)


__all__ = ["ResidualRecord", "ResidualLog", "DEFAULT_MAX_BYTES", "DEFAULT_GENERATIONS"]
