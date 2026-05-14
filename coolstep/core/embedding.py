"""Numeric frame → fixed-dim vector for ChromaDB.

Approach: TelemetryFrame's structured fields are flattened into a fixed schema
of named scalar features, then per-feature robustly normalized (median + MAD)
so cosine distance treats °C, MHz, RPM, %, V on equal footing, then L2-normalized
to unit length so cosine ≡ dot.

Why robust scaler instead of standard z-score:
- Outlier ticks (game launches, sudden 95°C spike) skew mean/std heavily.
- Median + MAD (median absolute deviation) ignores them.
- Same recipe sklearn's RobustScaler uses, but stdlib-only here.

Stats (median, MAD per feature) recomputed periodically by Embedder.refit() over
a sliding window of recent frames. Default first-fit happens after N frames;
until then `embed()` returns None (caller should skip vector write).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from coolstep.core.schema import TelemetryFrame

log = logging.getLogger(__name__)

FEATURE_NAMES: tuple[str, ...] = (
    "cpu_temp_max",
    "cpu_temp_avg",
    "cpu_freq_max",
    "cpu_freq_avg",
    "cpu_load_max",
    "cpu_load_avg",
    "cpu_load_p95",
    "cpu_power_pkg",
    "gpu_temp_max",
    "gpu_temp_avg",
    "gpu_power_max",
    "gpu_sclk_max",
    "gpu_util_max",
    "fan_rpm_max",
    "fan_rpm_avg",
    "storage_temp_max",
    "memory_temp_max",
    "visible_apps",
    "unique_classes",
    "epp_balance_power",      # binary 1.0 if epp == "balance_power"
    "epp_performance",        # binary 1.0 if epp == "performance"
    "profile_quiet",
    "profile_balanced",
    "profile_performance",
    "boost_on",               # binary
    "governor_perf",          # binary
)


@dataclass(slots=True)
class _Stat:
    median: float = 0.0
    mad: float = 1.0


def extract_raw(frame: TelemetryFrame) -> dict[str, float]:
    """Frame → dict of named raw scalars. Missing → 0.0 (cold start safe)."""
    out: dict[str, float] = dict.fromkeys(FEATURE_NAMES, 0.0)

    if frame.cpu.temps_c:
        out["cpu_temp_max"] = max(frame.cpu.temps_c.values())
        out["cpu_temp_avg"] = sum(frame.cpu.temps_c.values()) / len(frame.cpu.temps_c)
    if frame.cpu.freq_mhz:
        out["cpu_freq_max"] = max(frame.cpu.freq_mhz)
        out["cpu_freq_avg"] = sum(frame.cpu.freq_mhz) / len(frame.cpu.freq_mhz)
    if frame.cpu.load_pct:
        out["cpu_load_max"] = max(frame.cpu.load_pct)
        out["cpu_load_avg"] = sum(frame.cpu.load_pct) / len(frame.cpu.load_pct)
        out["cpu_load_p95"] = _percentile(frame.cpu.load_pct, 95)
    if frame.cpu.power_w:
        out["cpu_power_pkg"] = frame.cpu.power_w.get("package", 0.0)

    if frame.gpus:
        gpu_temps = [g.temp_c for g in frame.gpus if g.temp_c is not None]
        gpu_powers = [g.power_w for g in frame.gpus if g.power_w is not None]
        gpu_sclks = [g.sclk_mhz for g in frame.gpus if g.sclk_mhz is not None]
        gpu_utils = [g.util_pct for g in frame.gpus if g.util_pct is not None]
        if gpu_temps:
            out["gpu_temp_max"] = max(gpu_temps)
            out["gpu_temp_avg"] = sum(gpu_temps) / len(gpu_temps)
        if gpu_powers:
            out["gpu_power_max"] = max(gpu_powers)
        if gpu_sclks:
            out["gpu_sclk_max"] = max(gpu_sclks)
        if gpu_utils:
            out["gpu_util_max"] = max(gpu_utils)

    fan_rpms = [f.rpm for f in frame.fans if f.rpm is not None]
    if fan_rpms:
        out["fan_rpm_max"] = float(max(fan_rpms))
        out["fan_rpm_avg"] = sum(fan_rpms) / len(fan_rpms)

    if frame.storage_temps_c:
        out["storage_temp_max"] = max(frame.storage_temps_c.values())
    if frame.memory_temps_c:
        out["memory_temp_max"] = max(frame.memory_temps_c.values())

    if frame.workload is not None:
        out["visible_apps"] = frame.workload.rolling_features.get("visible_apps", 0.0)
        out["unique_classes"] = frame.workload.rolling_features.get("unique_classes", 0.0)

    state = frame.platform_state
    out["epp_balance_power"] = 1.0 if state.get("epp") == "balance_power" else 0.0
    out["epp_performance"] = 1.0 if state.get("epp") == "performance" else 0.0
    out["profile_quiet"] = 1.0 if state.get("platform_profile") == "quiet" else 0.0
    out["profile_balanced"] = 1.0 if state.get("platform_profile") == "balanced" else 0.0
    out["profile_performance"] = 1.0 if state.get("platform_profile") == "performance" else 0.0
    out["boost_on"] = 1.0 if state.get("boost") == "1" else 0.0
    out["governor_perf"] = 1.0 if state.get("governor") == "performance" else 0.0
    return out


class Embedder:
    """Holds robust normalization stats, embeds frame → unit-norm vector."""

    def __init__(self, min_frames_to_fit: int = 60) -> None:
        self._stats: dict[str, _Stat] = {n: _Stat() for n in FEATURE_NAMES}
        self._fitted = False
        self.min_frames_to_fit = min_frames_to_fit

    @property
    def fitted(self) -> bool:
        return self._fitted

    def refit(self, frames: list[TelemetryFrame]) -> None:
        if len(frames) < self.min_frames_to_fit:
            return
        rows = [extract_raw(f) for f in frames]
        for name in FEATURE_NAMES:
            values = sorted(r[name] for r in rows)
            n = len(values)
            median = values[n // 2] if n else 0.0
            abs_dev = sorted(abs(v - median) for v in values)
            mad = abs_dev[n // 2] if n else 1.0
            self._stats[name] = _Stat(median=median, mad=mad if mad > 1e-9 else 1.0)
        self._fitted = True

    def stats_snapshot(self) -> dict[str, dict[str, float]]:
        return {
            name: {"median": s.median, "mad": s.mad}
            for name, s in self._stats.items()
        }

    def save_stats(self, path: Path) -> None:
        """Persist median/MAD so a restarted daemon embeds compatibly with
        vectors already in chroma. Without this, post-restart refit on a
        different window skews normalization and breaks KNN consistency.
        """
        if not self._fitted:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "feature_names": list(FEATURE_NAMES),
            "stats": self.stats_snapshot(),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")))
        tmp.replace(path)

    def load_stats(self, path: Path) -> bool:
        """Return True if stats loaded и embedder помечен fitted. False иначе
        (file отсутствует, corrupt, или схема feature_names разъехалась)."""
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("embedder stats load failed: %r", exc)
            return False
        names = tuple(payload.get("feature_names", []))
        if names != FEATURE_NAMES:
            log.warning(
                "embedder stats schema mismatch — refusing to load "
                "(saved=%d names, current=%d)",
                len(names), len(FEATURE_NAMES),
            )
            return False
        stats = payload.get("stats", {})
        for name in FEATURE_NAMES:
            row = stats.get(name) or {}
            median = float(row.get("median", 0.0))
            mad = float(row.get("mad", 1.0)) or 1.0
            self._stats[name] = _Stat(median=median, mad=mad)
        self._fitted = True
        return True

    def embed(self, frame: TelemetryFrame) -> list[float] | None:
        if not self._fitted:
            return None
        raw = extract_raw(frame)
        scaled = [
            (raw[name] - self._stats[name].median) / self._stats[name].mad
            for name in FEATURE_NAMES
        ]
        norm = math.sqrt(sum(x * x for x in scaled))
        if norm < 1e-9:
            return [0.0] * len(FEATURE_NAMES)
        return [x / norm for x in scaled]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sv = sorted(values)
    idx = max(0, min(len(sv) - 1, int(round((pct / 100.0) * (len(sv) - 1)))))
    return sv[idx]
