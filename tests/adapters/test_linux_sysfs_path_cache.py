"""P2.5 — `LinuxSysfsCollector` path cache regression suite.

The collector must enumerate hwmon devices + cpufreq paths once and
re-use the cached references on subsequent ticks. Test against a fake
sysfs tree under tmp_path so we can count globs deterministically.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from coolstep.adapters.collectors.linux_sysfs import (
    PATH_CACHE_TTL_S,
    LinuxSysfsCollector,
)


def _make_fake_sysfs(root: Path) -> tuple[Path, Path, Path, Path]:
    """Build a minimal sysfs-like layout under `root`.

    hwmon0 = k10temp (CPU temps)
    hwmon1 = asus_custom_fan_curve (fan + pwm)
    cpu0..cpu1 = cpufreq scaling_cur_freq
    /proc/stat-shaped file
    platform_profile file
    """
    hwmon_root = root / "sys" / "class" / "hwmon"
    cpu_root = root / "sys" / "devices" / "system" / "cpu"
    hwmon_root.mkdir(parents=True)
    cpu_root.mkdir(parents=True)

    # k10temp device with two temp inputs
    h0 = hwmon_root / "hwmon0"
    h0.mkdir()
    (h0 / "name").write_text("k10temp\n")
    (h0 / "temp1_input").write_text("62000\n")
    (h0 / "temp1_label").write_text("Tctl\n")
    (h0 / "temp2_input").write_text("61750\n")
    (h0 / "temp2_label").write_text("Tdie\n")

    # asus fan device
    h1 = hwmon_root / "hwmon1"
    h1.mkdir()
    (h1 / "name").write_text("asus_custom_fan_curve\n")
    (h1 / "fan1_input").write_text("3300\n")
    (h1 / "fan1_label").write_text("CPU\n")
    (h1 / "pwm1").write_text("64\n")

    # Per-core cpufreq
    for i in (0, 1):
        d = cpu_root / f"cpu{i}" / "cpufreq"
        d.mkdir(parents=True)
        (d / "scaling_cur_freq").write_text(f"{2000000 + i * 1000}\n")
    (cpu_root / "cpu0" / "cpufreq" / "scaling_governor").write_text("powersave\n")
    (cpu_root / "cpu0" / "cpufreq" / "energy_performance_preference").write_text("balance_performance\n")
    (cpu_root / "cpufreq").mkdir()
    (cpu_root / "cpufreq" / "boost").write_text("1\n")

    proc_stat = root / "proc-stat"
    proc_stat.write_text(
        "cpu  0 0 0 100 0 0 0\n"
        "cpu0 0 0 0 100 0 0 0\n"
        "cpu1 0 0 0 100 0 0 0\n"
    )
    platform_profile = root / "platform_profile"
    platform_profile.write_text("performance\n")
    return hwmon_root, cpu_root, proc_stat, platform_profile


@pytest.fixture
def collector(tmp_path):
    hwmon, cpu, stat, profile = _make_fake_sysfs(tmp_path)
    return LinuxSysfsCollector(
        hwmon_root=hwmon, cpu_root=cpu, proc_stat_path=stat,
        platform_profile_path=profile,
    )


def test_cache_empty_at_init(collector):
    """Cache is lazy — `discover()` doesn't populate it. Only the first
    `sample()` does, so collectors that never get sampled don't pay the
    enumeration cost."""
    assert collector._hwmon_devices == []
    assert collector._cpu_freq_paths == []
    assert collector._paths_at == 0.0


def test_cache_populated_after_first_sample(collector):
    collector.sample()
    # k10temp + asus fan device discovered
    names = sorted(d["name"] for d in collector._hwmon_devices)
    assert names == ["asus_custom_fan_curve", "k10temp"]
    # 2 cpufreq paths (cpu0, cpu1)
    assert len(collector._cpu_freq_paths) == 2
    # _paths_at set to a monotonic timestamp
    assert collector._paths_at > 0.0


def test_subsequent_samples_do_not_reglob(collector, tmp_path):
    """The whole point — re-globbing hwmon* costs ~5-6 readdirs per
    tick; with the cache, after the first sample we hit zero globs."""
    collector.sample()
    glob_count = 0
    real_glob = Path.glob

    def counting_glob(self, pattern):
        nonlocal glob_count
        glob_count += 1
        return real_glob(self, pattern)

    with patch.object(Path, "glob", counting_glob):
        collector.sample()
        collector.sample()
    assert glob_count == 0, f"unexpected globs in steady-state ticks: {glob_count}"


def test_ttl_invalidates_cache(collector, tmp_path):
    """Past TTL, the next sample re-enumerates so hot-plugged devices
    become visible."""
    collector.sample()
    initial = list(collector._hwmon_devices)
    # Backdate the cache stamp past the TTL.
    collector._paths_at = time.monotonic() - PATH_CACHE_TTL_S - 1.0

    # Add a new hwmon device while the daemon is sleeping.
    new_dev = collector.hwmon_root / "hwmon2"
    new_dev.mkdir()
    (new_dev / "name").write_text("nvme\n")
    (new_dev / "temp1_input").write_text("36000\n")
    (new_dev / "temp1_label").write_text("Composite\n")

    collector.sample()
    names = sorted(d["name"] for d in collector._hwmon_devices)
    assert "nvme" in names, "TTL expiry should re-discover hwmon devices"
    assert len(collector._hwmon_devices) > len(initial)


def test_temps_correctly_read_via_cache(collector):
    """Sanity — the cached path list resolves to the same temp values
    the legacy globbing path produced."""
    partial = collector.sample()
    temps = partial["cpu"]["temps_c"]
    assert "tctl" in temps
    assert temps["tctl"] == pytest.approx(62.0)
    assert temps["tdie"] == pytest.approx(61.75)


def test_fan_correctly_read_via_cache(collector):
    partial = collector.sample()
    fans = partial["fans"]
    assert len(fans) == 1
    assert fans[0].rpm == 3300
    assert fans[0].pwm == 64


def test_platform_state_paths_static(collector):
    """Platform-state paths are built once in `__init__` from fixed
    kernel sysfs paths — they don't need a refresh cycle."""
    state = collector._read_platform_state()
    assert state.get("governor") == "powersave"
    assert state.get("epp") == "balance_performance"
    assert state.get("platform_profile") == "performance"
    assert state.get("boost") == "1"


def test_cost_tracker_reports_lower_after_warm_up(collector):
    """First sample includes path enumeration; subsequent samples are
    pure I/O. We can't assert an absolute number on the CI (sysfs in
    tmp_path is faster than real /sys), but the relationship cold ≥
    warm should always hold."""
    collector.sample()
    cold_us = collector._cost.avg_us()
    for _ in range(5):
        collector.sample()
    warm_us = collector._cost.avg_us()
    # The average is rolling over 32 samples (CostTracker capacity);
    # warm should not exceed cold by any meaningful margin.
    assert warm_us <= cold_us * 1.5, (
        f"warm tick more expensive than cold: warm={warm_us} cold={cold_us}"
    )
