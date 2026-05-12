"""Workload fingerprint extraction from rolling-window TelemetryFrames.

Extracts coarse signals usable for both unsupervised clustering (P0) and
supervised prediction (P1). Pure-Python, stdlib only — no numpy at this layer
(adapters can rely on heavier deps; core stays portable).

Features extracted (per call):
- cpu_load_max, cpu_load_avg, cpu_load_p95
- cpu_temp_max, cpu_temp_avg, cpu_temp_slope_per_sec (linear fit)
- gpu_temp_max, gpu_temp_avg
- fan_rpm_max, fan_rpm_avg
- visible_apps, unique_classes (carried from hyprctl rolling)
- peak_count: number of CPU load spikes (>80%) in window
- spike_density: peak_count / window_seconds
- heat_soak_index: ΔT/ΔPower (load fallback) — unit-free «inertia» ratio.
  NOT in `embedding.FEATURE_NAMES` on purpose: keeps the chroma embedding
  schema stable, the daemon passes this as a separate scalar.
"""

from __future__ import annotations

from collections.abc import Sequence

from coolstep.core.schema import TelemetryFrame


def extract(window: Sequence[TelemetryFrame]) -> dict[str, float]:
    if not window:
        return {}

    loads_per_frame = [_avg(f.cpu.load_pct) for f in window]
    cpu_temps = [_tctl(f) for f in window]
    cpu_temps = [t for t in cpu_temps if t is not None]
    gpu_temps = [
        max((g.temp_c for g in f.gpus if g.temp_c is not None), default=None) for f in window
    ]
    gpu_temps_clean = [t for t in gpu_temps if t is not None]
    fan_rpms = [
        max((fan.rpm for fan in f.fans if fan.rpm is not None), default=None) for f in window
    ]
    fan_rpms_clean = [r for r in fan_rpms if r is not None]

    out: dict[str, float] = {}
    if loads_per_frame:
        out["cpu_load_max"] = max(loads_per_frame)
        out["cpu_load_avg"] = _avg(loads_per_frame)
        out["cpu_load_p95"] = _percentile(loads_per_frame, 95)
        out["peak_count"] = float(sum(1 for L in loads_per_frame if L > 80.0))
    temp_slope: float | None = None
    if cpu_temps:
        out["cpu_temp_max"] = max(cpu_temps)
        out["cpu_temp_avg"] = _avg(cpu_temps)
        temp_slope = _slope(window, cpu_temps)
        if temp_slope is not None:
            out["cpu_temp_slope_per_sec"] = temp_slope
    # Heat-soak: how much temperature is moving per unit of forcing input.
    # Primary path: ΔT/ΔPower (W/s). Falls back to ΔT/Δload when k10temp
    # doesn't expose RAPL — Ryzen 7940HS state today. Returns 0.0 when
    # neither signal moves: «no information» rather than fake aliveness.
    if temp_slope is not None and len(window) >= 2:
        cpu_powers = [
            f.cpu.power_w.get("package", 0.0) for f in window
        ]
        power_slope = _slope(window, cpu_powers)
        if power_slope is not None and abs(power_slope) > 1.0:
            out["heat_soak_index"] = temp_slope / power_slope
        else:
            load_slope = _slope(window, loads_per_frame)
            if load_slope is not None and abs(load_slope) > 1.0:
                out["heat_soak_index"] = temp_slope / load_slope
            else:
                out["heat_soak_index"] = 0.0
    if gpu_temps_clean:
        out["gpu_temp_max"] = max(gpu_temps_clean)
        out["gpu_temp_avg"] = _avg(gpu_temps_clean)
    if fan_rpms_clean:
        out["fan_rpm_max"] = float(max(fan_rpms_clean))
        out["fan_rpm_avg"] = _avg([float(r) for r in fan_rpms_clean])

    last = window[-1]
    if last.workload is not None:
        out["visible_apps"] = last.workload.rolling_features.get("visible_apps", 0.0)
        out["unique_classes"] = last.workload.rolling_features.get("unique_classes", 0.0)

    if len(window) >= 2:
        duration = window[-1].timestamp - window[0].timestamp
        if duration > 0 and "peak_count" in out:
            out["spike_density"] = out["peak_count"] / duration

    return out


def _avg(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = max(0, min(len(sorted_vals) - 1, int(round((pct / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def _tctl(frame: TelemetryFrame) -> float | None:
    """Pick the best 'representative CPU temperature' for this frame.

    Cross-vendor: AMD k10temp emits `tctl`/`tdie`; Intel coretemp emits
    `package id 0` / `physical id 0` / `core 0..N`; ARM thermal zones
    emit `x86_pkg_temp` or `cpu-thermal`.  We try keys in priority
    order and fall back to the max across whatever the dict actually
    contains so the trajectory-fallback signal still fires on Intel
    and ARM hosts.
    """
    temps = frame.cpu.temps_c
    if not temps:
        return None
    # Preferred package-level keys, in priority order.
    for key in ("tctl", "tdie", "package id 0", "package", "x86_pkg_temp"):
        if key in temps:
            return temps[key]
    # Match prefixes for vendor-specific naming.
    for key, val in temps.items():
        kl = key.lower()
        if kl.startswith("package") or kl.startswith("cpu") or kl.startswith("physical id"):
            return val
    # Last resort: max across all reported temps. Better than None — keeps
    # trajectory_fallback alive on hosts with non-standard sensor names.
    return max(temps.values())


def _slope(window: Sequence[TelemetryFrame], values: Sequence[float]) -> float | None:
    """OLS slope of `values` vs frame timestamps. Returns None if degenerate."""
    if len(window) < 2 or len(values) != len(window):
        return None
    xs = [f.timestamp for f in window]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    num = sum((xs[i] - mean_x) * (values[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    if den == 0.0:
        return None
    return num / den
