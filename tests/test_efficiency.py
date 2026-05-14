"""Efficiency curve tests."""

from __future__ import annotations

import time

from coolstep.core.efficiency import (
    TEMP_BUCKET_C,
    compute_historical,
    compute_live,
)
from coolstep.core.schema import (
    CpuMetrics,
    GpuMetrics,
    TelemetryFrame,
)
from coolstep.core.store import Store


def test_live_efficiency_basic():
    eff = compute_live(load_avg_pct=50.0, freq_avg_mhz=4000.0,
                       cpu_temp_c=70.0, t_ambient=30.0)
    # work = 0.5 × 4 × 100 = 200; headroom = 40 → 5.0
    assert eff == 5.0


def test_live_efficiency_clamps_headroom_to_one():
    """At T == ambient, division-by-zero avoided."""
    eff = compute_live(50.0, 4000.0, 30.0, t_ambient=30.0)
    assert eff > 0
    assert eff != float("inf")


def test_live_efficiency_zero_at_idle():
    eff = compute_live(0.0, 4000.0, 70.0, t_ambient=30.0)
    assert eff == 0.0


def test_historical_with_no_store(tmp_path):
    rep = compute_historical(tmp_path / "absent.db")
    assert rep.bins == []
    assert rep.sample_count == 0


def test_historical_finds_sweet_spot(tmp_path):
    """Synthesise frames where 65°C bucket has highest efficiency."""
    store = Store(tmp_path / "store.db")
    base_ts = time.time() - 1000
    for i in range(50):
        # Vary cpu_temp + freq так, что у tctl=65 efficiency максимальная
        if i < 15:
            tctl, freq, load = 50.0, 3000.0, 30.0   # cool but low work
        elif i < 35:
            tctl, freq, load = 65.0, 4500.0, 80.0   # hot work, high efficiency
        else:
            tctl, freq, load = 90.0, 4500.0, 95.0   # hotter, work flat
        frame = TelemetryFrame(
            timestamp=base_ts + i,
            cpu=CpuMetrics(
                freq_mhz=[freq] * 4,
                load_pct=[load] * 4,
                temps_c={"tctl": tctl},
            ),
            gpus=[GpuMetrics(name="g", temp_c=50.0)],
        )
        store.write_frame(frame)
    store.close()

    rep = compute_historical(tmp_path / "store.db")
    assert rep.sample_count >= 40
    assert rep.sweet_spot_temp is not None
    # 65°C bucket midpoint is 65 (bucket [64,66))
    assert 60 <= rep.sweet_spot_temp <= 70


def test_bucket_width_constant():
    assert TEMP_BUCKET_C == 2.0


def test_historical_handles_corrupt_raw_json(tmp_path):
    """Frames с corrupt raw_json пропускаются, но остальные считаются."""
    store = Store(tmp_path / "store.db")
    base_ts = time.time() - 100
    # 1 валидный
    store.write_frame(
        TelemetryFrame(
            timestamp=base_ts,
            cpu=CpuMetrics(freq_mhz=[4000.0], load_pct=[50.0], temps_c={"tctl": 70.0}),
        )
    )
    store.close()
    # Inject corrupt row direct
    import sqlite3
    conn = sqlite3.connect(tmp_path / "store.db")
    conn.execute(
        "INSERT INTO frames (ts, cpu_temp, raw_json) VALUES (?, ?, ?)",
        (base_ts + 1, 70.0, "{not json"),
    )
    conn.commit()
    conn.close()

    rep = compute_historical(tmp_path / "store.db")
    assert rep.sample_count == 1
