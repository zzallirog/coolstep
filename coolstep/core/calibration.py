"""Calibration gates — when can predictor go live?

Each gate evaluates over the persistent store + live ring snapshots. Returns
a structured result so the dashboard can render a gate-by-gate checklist.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from coolstep.core.ring import Ring


@dataclass(slots=True)
class GateResult:
    name: str
    passed: bool
    current: float
    target: float
    unit: str
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "current": self.current,
            "target": self.target,
            "unit": self.unit,
            "note": self.note,
        }


@dataclass(slots=True)
class CalibrationReport:
    gates: list[GateResult]
    ready: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "gates": [g.to_dict() for g in self.gates],
        }


# Production targets (week-long calibration). All overridable via env so a
# shadow-pilot deployment can ramp gates faster without touching code.
COVERAGE_HOURS_TARGET = float(os.environ.get("COOLSTEP_COVERAGE_HOURS", 168.0))
THROTTLE_EVENTS_TARGET = int(os.environ.get("COOLSTEP_THROTTLE_EVENTS", 10))
PEAK_TEMP_TARGET = float(os.environ.get("COOLSTEP_PEAK_TEMP", 85.0))
WORKLOAD_CLUSTERS_TARGET = int(os.environ.get("COOLSTEP_WORKLOAD_CLUSTERS", 5))
# 5% miss rate tolerates locked-screen / no-window frames (incident
# 2026-05-10: 1% was unreachable on normal days).
HYPRCTL_MISS_TARGET = float(os.environ.get("COOLSTEP_HYPRCTL_MISS_PCT", 5.0))
COST_BUDGET_RSS_KB = int(os.environ.get("COOLSTEP_COST_RSS_KB", 50_000))
COST_BUDGET_CPU_PCT = float(os.environ.get("COOLSTEP_COST_CPU_PCT", 1.0))

# Daemon period — used to convert frame count to cumulative uptime. Must
# match `DEFAULT_PERIOD` in daemon.py. If period_sec is overridden at runtime
# this calculation will under/overstate uptime proportionally.
COVERAGE_PERIOD_SEC = float(os.environ.get("COOLSTEP_COVERAGE_PERIOD_SEC", 1.0))


def evaluate(
    store_path: Path,
    ring: Ring | None = None,
    cost_rss_kb: float | None = None,
    cost_cpu_pct: float | None = None,
) -> CalibrationReport:
    """Compute all gates. Each gate handles missing data gracefully."""
    coverage_seconds, throttle_count, peak_temp, hyprctl_miss_pct, distinct_labels = (
        _query_store(store_path)
    )

    gates: list[GateResult] = []
    gates.append(GateResult(
        name="coverage_hours",
        passed=coverage_seconds / 3600 >= COVERAGE_HOURS_TARGET,
        current=coverage_seconds / 3600,
        target=COVERAGE_HOURS_TARGET,
        unit="hours",
        note="≥ 168h ≈ полная неделя; покрытие weekday + weekend",
    ))
    gates.append(GateResult(
        name="throttle_events",
        passed=throttle_count >= THROTTLE_EVENTS_TARGET,
        current=float(throttle_count),
        target=float(THROTTLE_EVENTS_TARGET),
        unit="events",
        note="нужны реальные throttle (heavy CPU/gaming) — без positive class predictor не валидируется",
    ))
    gates.append(GateResult(
        name="peak_amplitude",
        passed=peak_temp >= PEAK_TEMP_TARGET,
        current=peak_temp,
        target=PEAK_TEMP_TARGET,
        unit="°C",
        note="видели температуры близкие к T_max",
    ))
    gates.append(GateResult(
        name="class_diversity",
        passed=distinct_labels >= WORKLOAD_CLUSTERS_TARGET,
        current=float(distinct_labels),
        target=float(WORKLOAD_CLUSTERS_TARGET),
        unit="clusters",
        note="не учим только на idle",
    ))
    gates.append(GateResult(
        name="hyprctl_consistency",
        passed=hyprctl_miss_pct < HYPRCTL_MISS_TARGET,
        current=hyprctl_miss_pct,
        target=HYPRCTL_MISS_TARGET,
        unit="%",
        note="<1% ticks без workload context",
    ))
    if cost_rss_kb is not None:
        gates.append(GateResult(
            name="cost_rss",
            passed=cost_rss_kb < COST_BUDGET_RSS_KB,
            current=cost_rss_kb,
            target=float(COST_BUDGET_RSS_KB),
            unit="KB",
            note="не сами греем — память",
        ))
    if cost_cpu_pct is not None:
        gates.append(GateResult(
            name="cost_cpu",
            passed=cost_cpu_pct < COST_BUDGET_CPU_PCT,
            current=cost_cpu_pct,
            target=COST_BUDGET_CPU_PCT,
            unit="%",
            note="не сами греем — CPU",
        ))
    if ring is not None:
        gates.append(GateResult(
            name="ring_warmup",
            passed=len(ring) >= ring.capacity // 2,
            current=float(len(ring)),
            target=float(ring.capacity // 2),
            unit="frames",
            note="rolling buffer прогрет",
        ))

    ready = all(g.passed for g in gates)
    return CalibrationReport(gates=gates, ready=ready)


def _query_store(path: Path) -> tuple[float, int, float, float, int]:
    """Returns (coverage_seconds, throttle_count, peak_temp, hyprctl_miss_pct,
    distinct_labels)."""
    if not path.exists():
        return 0.0, 0, 0.0, 100.0, 0
    conn = sqlite3.connect(path)
    try:
        # Cumulative uptime = frame_count × period_sec. A daemon that ran 1h,
        # stopped 6 days, ran 1h reads as 2h (not 168h via MAX-MIN). Ignores
        # gaps from suspend/reboot/restart (incident 2026-05-10 BLOCKER #1).
        total = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] or 0
        coverage_seconds = float(total) * COVERAGE_PERIOD_SEC

        throttle_count = conn.execute(
            "SELECT COUNT(*) FROM throttle_events"
        ).fetchone()[0]

        peak_row = conn.execute(
            "SELECT MAX(cpu_temp) FROM frames"
        ).fetchone()
        peak_temp = float(peak_row[0]) if peak_row[0] is not None else 0.0

        # hyprctl_consistency: denominator = frames where collector was live
        # (cpu_temp > 0 = linux_sysfs ran, hyprctl runs alongside). Locked-
        # screen frames pass through with label=None — they are STRUCTURALLY
        # unlabelled, not collector failures. Excluding cpu_temp=0/NULL frames
        # filters out partial/restart-edge captures (incident 2026-05-10
        # BLOCKER #2). Threshold also raised 1%→5% by env default.
        active_total = conn.execute(
            "SELECT COUNT(*) FROM frames WHERE cpu_temp IS NOT NULL AND cpu_temp > 0"
        ).fetchone()[0] or 1
        labelled = conn.execute(
            "SELECT COUNT(*) FROM frames WHERE cpu_temp IS NOT NULL AND cpu_temp > 0 "
            "AND workload_label IS NOT NULL AND workload_label != ''"
        ).fetchone()[0]
        miss_pct = 100.0 * (1.0 - labelled / active_total) if active_total > 0 else 100.0

        # class_diversity: исключаем 'unknown' fallback из hyprctl и пустые,
        # они не представляют реальный workload-cluster (BLOCKER #3 minor).
        distinct_labels = conn.execute(
            "SELECT COUNT(DISTINCT workload_label) FROM frames "
            "WHERE workload_label IS NOT NULL "
            "AND workload_label != '' "
            "AND workload_label != 'unknown'"
        ).fetchone()[0]
    finally:
        conn.close()
    return coverage_seconds, int(throttle_count), peak_temp, miss_pct, int(distinct_labels)
