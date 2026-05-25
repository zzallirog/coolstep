"""Workload fingerprint extraction from rolling-window TelemetryFrames.

Extracts coarse signals usable for both unsupervised clustering (P0) and
supervised prediction (P1). Pure-Python, stdlib only — no numpy at this layer
(adapters can rely on heavier deps; core stays portable).

Features extracted (per call):
- cpu_load_max, cpu_load_avg, cpu_load_p95
- cpu_temp_now (last frame, "live"), cpu_temp_max, cpu_temp_avg,
  cpu_temp_avg_5min            (mean over the last 300 s of frames —
                                 fixed-duration window for the meta-bucket
                                 phase axis; insensitive to ring capacity)
  cpu_temp_slope_per_sec       (linear fit over full window — slow, for
                                 bucket-key in residual meta-learner)
  cpu_temp_slope_per_sec_short (linear fit over last ~5 frames — fast,
                                 for predictor T0+slope·τ extrapolation
                                 and "dT/dt" cockpit readout)
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
    cpu_temps_seq = [_tctl(f) for f in window]
    # "now" = latest non-None reading.  Distinct from cpu_temp_max which is
    # max() over the whole rolling window and so latches onto past peaks
    # for as long as the window is wide — caused predictor + cockpit to
    # display 80°C while reality cooled to 62°C (ADR pending).
    cpu_temp_now = next((t for t in reversed(cpu_temps_seq) if t is not None), None)
    cpu_temps = [t for t in cpu_temps_seq if t is not None]
    # Short-window pair: last N frames *with* non-None temp, preserving
    # frame↔value alignment so the slope OLS gets matching timestamps.
    # 5 frames ≈ 5 s on the 1 Hz collector — short enough to expose
    # transient ramps (predictor T0+slope·τ needs a "real" instantaneous
    # slope; the 600-frame window slope is essentially zero through
    # bidirectional jitter).
    _short_n = 5
    short_pairs = [
        (f, t) for f, t in zip(window, cpu_temps_seq, strict=False) if t is not None
    ][-_short_n:]
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
        # Load slope — used by the meta-predictor's bucket key.  When
        # load is dropping faster than -0.5 %/s, the chip is about to
        # cool no matter what the current temp slope says — that's the
        # missing signal behind the −20°C overshoot the user observed.
        load_slope_for_bucket = _slope(window, loads_per_frame)
        if load_slope_for_bucket is not None:
            out["cpu_load_slope_per_sec"] = load_slope_for_bucket
    temp_slope: float | None = None
    if cpu_temps:
        out["cpu_temp_max"] = max(cpu_temps)
        out["cpu_temp_avg"] = _avg(cpu_temps)
        if cpu_temp_now is not None:
            out["cpu_temp_now"] = cpu_temp_now
        # Fixed 5-min trailing mean (relative to the latest frame's
        # timestamp) — used by `residual_meta.quantise_temp_phase` to
        # tag bucket regime as ascending / plateau / descending. Computed
        # by timestamp (not frame count) so the answer is stable across
        # collector jitter and any future ring-capacity tweak. Emitted
        # only when ≥ 2 frames in the 5-min window carry a temp reading;
        # otherwise the phase axis falls back to plateau (safe default).
        cutoff_ts = window[-1].timestamp - 300.0
        temps_5min = [
            t for f, t in zip(window, cpu_temps_seq, strict=False)
            if t is not None and f.timestamp >= cutoff_ts
        ]
        if len(temps_5min) >= 2:
            out["cpu_temp_avg_5min"] = _avg(temps_5min)
        temp_slope = _slope(window, cpu_temps)
        if temp_slope is not None:
            out["cpu_temp_slope_per_sec"] = temp_slope
        if len(short_pairs) >= 2:
            short_frames = [f for f, _ in short_pairs]
            short_vals = [t for _, t in short_pairs]
            short_slope = _slope(short_frames, short_vals)
            if short_slope is not None:
                out["cpu_temp_slope_per_sec_short"] = short_slope
        # Second derivative — d²T/dt² over the last half of the window.
        # We compute it as the slope-of-the-slope across two halves:
        # split the window in two, fit each, take difference / span.
        # Positive accel = ramp accelerating (heat soak not yet at
        # equilibrium); negative = ramp decelerating (approaching
        # asymptote OR active cooling kicked in).  Cheap to compute,
        # mostly stdlib, signal-to-noise ok at window >= 6 frames.
        accel = _accel(window, cpu_temps)
        if accel is not None:
            out["cpu_temp_accel_per_sec_sq"] = accel
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


def _accel(window: Sequence[TelemetryFrame], values: Sequence[float]) -> float | None:
    """Acceleration: slope-of-slope, by splitting the window in halves
    and taking (slope_second_half − slope_first_half) / (midpoint_dt).

    Returns None when window has < 6 frames (need at least 3 per half
    for a non-degenerate slope each side).  Units: °C/s².
    """
    n = len(window)
    if n < 6 or len(values) != n:
        return None
    half = n // 2
    s1 = _slope(window[:half], values[:half])
    s2 = _slope(window[half:], values[half:])
    if s1 is None or s2 is None:
        return None
    mid1 = (window[0].timestamp + window[half - 1].timestamp) / 2.0
    mid2 = (window[half].timestamp + window[-1].timestamp) / 2.0
    dt = mid2 - mid1
    if dt <= 0:
        return None
    return (s2 - s1) / dt
