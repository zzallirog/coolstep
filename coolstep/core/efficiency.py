"""Thermal-efficiency proxy: useful_work / thermal_headroom.

Idea (см. docs/efficiency-curve.md): на CMOS перегиб leakage-vs-temp экспоненциальный
(Arrhenius, ADR-013). Полупроводник держит КПД до некоторой T*, потом резкий спад.
Точка перегиба зависит от чипа + теплоинтерфейса; **паттерн общий**.

Метрика, не требующая CPU package power (которого нет на AMD k10temp):

    useful_work       = mean(load_pct) × mean(freq_mhz) / 1000     # GHz × util
    thermal_headroom  = T_max - T_chip                              # °C, T_max=100
    work_per_degree   = useful_work / (T_chip - T_ambient)          # GHz·% / °C-above-baseline

Где:
- T_max = 100°C (Tjunction предел Ryzen 7940HS)
- T_ambient = 30°C (комнатная baseline; конфигурируемо через env COOLSTEP_T_AMBIENT)

work_per_degree высокий = CPU делает много работы при малом нагреве (хороший КПД).
Низкий = либо idle (нет работы), либо thermal-bound (много градусов на единицу work).

Historical: бакеты по cpu_temp_max шириной 2°C, считаем mean work_per_degree
+ count + percentiles. Sweet spot = arg max bins[work_per_degree].
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

T_MAX_DEFAULT = 100.0
T_AMBIENT_DEFAULT = 30.0
TEMP_BUCKET_C = 2.0  # bucket width


def _t_ambient() -> float:
    return float(os.environ.get("COOLSTEP_T_AMBIENT", T_AMBIENT_DEFAULT))


@dataclass(slots=True, frozen=True)
class EfficiencyBin:
    temp_low: float           # °C, lower edge
    temp_high: float
    samples: int
    mean_efficiency: float    # GHz·% / °C-above-ambient
    p50_efficiency: float
    p95_efficiency: float


@dataclass(slots=True, frozen=True)
class EfficiencyReport:
    bins: list[EfficiencyBin]
    sweet_spot_temp: float | None        # midpoint of bin with max mean_efficiency
    sweet_spot_efficiency: float | None
    knee_temp: float | None              # first bin below sweet spot - 30%
    sample_count: int
    t_ambient: float
    t_max: float


def compute_live(load_avg_pct: float, freq_avg_mhz: float,
                 cpu_temp_c: float, t_ambient: float | None = None) -> float:
    amb = t_ambient if t_ambient is not None else _t_ambient()
    headroom = max(1.0, cpu_temp_c - amb)  # avoid div-by-0 on cold-boot
    useful = (load_avg_pct / 100.0) * (freq_avg_mhz / 1000.0) * 100.0  # 0..100 GHz·%
    return useful / headroom


def compute_historical(store_path: Path, since_seconds: float = 7 * 86400) -> EfficiencyReport:
    """Bin frames by cpu_temp_max and compute efficiency stats.

    Reads sqlite directly. Uses raw_json for load/freq access since shortcut
    columns don't carry per-core arrays.
    """
    if not store_path.exists():
        return EfficiencyReport([], None, None, None, 0, _t_ambient(), T_MAX_DEFAULT)
    import json
    import time

    # 2026-05-14 incident: on a populated store (300k+ frames in 24h at
    # 5-10 Hz tick rate) the unbounded scan + json.loads each row built
    # ~3M Python objects per call and OOMed the dashboard. Cap at 20k
    # rows — for efficiency-curve binning that's >>10× the precision we
    # surface (TEMP_BUCKET_C resolution × handful of cpu_temp buckets).
    # Take the most recent N then re-order ascending so downstream
    # binning sees natural time order.
    HISTORICAL_ROW_CAP = 20_000

    cutoff = time.time() - since_seconds
    # 2026-05-14: connect read-only via URI. Default sqlite3.connect opens
    # for read+write which under WAL needs a writable `-shm` mmap; that
    # path intermittently failed with "disk I/O error" on the FastAPI
    # worker thread (store.db at ~1 GB, -wal at 36 MB, contended by the
    # live writer). `mode=ro` opts into the WAL-reader-without-shm path
    # and is correct anyway — this function never writes.
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        # No outer ORDER BY: downstream code only bins by cpu_temp, never
        # uses row order. The inner ORDER BY ts DESC + LIMIT walks
        # idx_frames_ts backwards (cheap). Adding an outer ORDER BY ts
        # forces `USE TEMP B-TREE FOR ORDER BY` — sqlite writes a temp
        # sort file → fights `PrivateTmp=true` in the systemd namespace
        # → "disk I/O error". Discovered 2026-05-14 via EXPLAIN QUERY PLAN.
        rows = conn.execute(
            "SELECT cpu_temp, raw_json FROM frames "
            "WHERE ts >= ? AND cpu_temp IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?",
            (cutoff, HISTORICAL_ROW_CAP),
        ).fetchall()
    finally:
        conn.close()

    amb = _t_ambient()
    samples_per_bin: dict[int, list[float]] = {}
    total = 0
    for cpu_temp, raw in rows:
        try:
            blob = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        loads = blob.get("cpu", {}).get("load_pct") or []
        freqs = blob.get("cpu", {}).get("freq_mhz") or []
        if not loads or not freqs:
            continue
        load_avg = sum(loads) / len(loads)
        freq_avg = sum(freqs) / len(freqs)
        eff = compute_live(load_avg, freq_avg, cpu_temp, amb)
        bucket = int(cpu_temp // TEMP_BUCKET_C)
        samples_per_bin.setdefault(bucket, []).append(eff)
        total += 1

    bins: list[EfficiencyBin] = []
    for bucket, vals in sorted(samples_per_bin.items()):
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        mean_eff = sum(vals_sorted) / n
        p50 = vals_sorted[n // 2]
        p95_idx = max(0, min(n - 1, int(0.95 * (n - 1))))
        p95 = vals_sorted[p95_idx]
        bins.append(
            EfficiencyBin(
                temp_low=bucket * TEMP_BUCKET_C,
                temp_high=(bucket + 1) * TEMP_BUCKET_C,
                samples=n,
                mean_efficiency=mean_eff,
                p50_efficiency=p50,
                p95_efficiency=p95,
            )
        )

    sweet_spot_temp: float | None = None
    sweet_spot_eff: float | None = None
    knee_temp: float | None = None
    if bins:
        # Filter bins with at least 5 samples to avoid sparse-bucket noise
        valid = [b for b in bins if b.samples >= 5]
        if valid:
            sweet = max(valid, key=lambda b: b.mean_efficiency)
            sweet_spot_temp = (sweet.temp_low + sweet.temp_high) / 2
            sweet_spot_eff = sweet.mean_efficiency
            # Knee: first bin past sweet where efficiency drops to <70% of peak
            past = [b for b in valid if b.temp_low >= sweet.temp_high]
            knee_threshold = sweet_spot_eff * 0.7
            for b in past:
                if b.mean_efficiency < knee_threshold:
                    knee_temp = (b.temp_low + b.temp_high) / 2
                    break

    return EfficiencyReport(
        bins=bins,
        sweet_spot_temp=sweet_spot_temp,
        sweet_spot_efficiency=sweet_spot_eff,
        knee_temp=knee_temp,
        sample_count=total,
        t_ambient=amb,
        t_max=T_MAX_DEFAULT,
    )
