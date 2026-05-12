"""linux_sysfs collector — fakeroot integration tests."""

from __future__ import annotations

from pathlib import Path

from coolstep.adapters.collectors.linux_sysfs import LinuxSysfsCollector


def _w(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _build_fake_sysfs(root: Path) -> tuple[Path, Path, Path, Path]:
    hwmon = root / "class" / "hwmon"
    cpu_root = root / "devices" / "system" / "cpu"
    proc_stat = root / "proc" / "stat"
    platform_profile = root / "firmware" / "acpi" / "platform_profile"

    # k10temp: tctl 78400 / tdie 79100
    _w(hwmon / "hwmon0" / "name", "k10temp")
    _w(hwmon / "hwmon0" / "temp1_label", "Tctl")
    _w(hwmon / "hwmon0" / "temp1_input", "78400")
    _w(hwmon / "hwmon0" / "temp2_label", "Tdie")
    _w(hwmon / "hwmon0" / "temp2_input", "79100")

    # nvme: 33000
    _w(hwmon / "hwmon1" / "name", "nvme")
    _w(hwmon / "hwmon1" / "temp1_label", "Composite")
    _w(hwmon / "hwmon1" / "temp1_input", "33000")

    # spd5118: 34000
    _w(hwmon / "hwmon2" / "name", "spd5118")
    _w(hwmon / "hwmon2" / "temp1_input", "34000")

    # asus: cpu_fan + gpu_fan
    _w(hwmon / "hwmon3" / "name", "asus")
    _w(hwmon / "hwmon3" / "fan1_label", "cpu_fan")
    _w(hwmon / "hwmon3" / "fan1_input", "2900")
    _w(hwmon / "hwmon3" / "fan2_label", "gpu_fan")
    _w(hwmon / "hwmon3" / "fan2_input", "2950")

    # ACAD — должен быть skip
    _w(hwmon / "hwmon4" / "name", "ACAD")
    _w(hwmon / "hwmon4" / "temp1_input", "99999")

    # cpufreq: 4 fake cores
    for i in range(4):
        _w(cpu_root / f"cpu{i}" / "cpufreq" / "scaling_cur_freq", str(3000_000 + i * 100_000))
    _w(cpu_root / "cpu0" / "cpufreq" / "scaling_governor", "performance")
    _w(cpu_root / "cpu0" / "cpufreq" / "energy_performance_preference", "performance")
    _w(cpu_root / "cpufreq" / "boost", "1")
    _w(platform_profile, "performance")

    # /proc/stat — 4 cores, idle=8 active=2 → 80% idle на первый sample, потом дельты
    _w(
        proc_stat,
        "\n".join(
            [
                "cpu  10 0 5 80 0 0 0 0 0 0",
                "cpu0 2 0 1 20 0 0 0 0 0 0",
                "cpu1 3 0 1 19 0 0 0 0 0 0",
                "cpu2 2 0 2 21 0 0 0 0 0 0",
                "cpu3 3 0 1 20 0 0 0 0 0 0",
                "intr 0",
                "",
            ]
        ),
    )

    return hwmon, cpu_root, proc_stat, platform_profile


def test_discover_returns_true_for_real_paths(tmp_path):
    hwmon, cpu_root, proc_stat, profile = _build_fake_sysfs(tmp_path)
    c = LinuxSysfsCollector(hwmon, cpu_root, proc_stat, profile)
    assert c.discover()


def test_discover_false_when_paths_missing(tmp_path):
    c = LinuxSysfsCollector(
        hwmon_root=tmp_path / "absent",
        cpu_root=tmp_path / "absent2",
        proc_stat_path=tmp_path / "stat",
        platform_profile_path=tmp_path / "pp",
    )
    assert not c.discover()


def test_sample_collects_cpu_temps(tmp_path):
    hwmon, cpu_root, proc_stat, profile = _build_fake_sysfs(tmp_path)
    c = LinuxSysfsCollector(hwmon, cpu_root, proc_stat, profile)
    partial = c.sample()
    cpu = partial["cpu"]
    assert isinstance(cpu, dict)
    temps = cpu["temps_c"]
    assert isinstance(temps, dict)
    assert temps["tctl"] == 78.4
    assert temps["tdie"] == 79.1


def test_sample_collects_freqs(tmp_path):
    hwmon, cpu_root, proc_stat, profile = _build_fake_sysfs(tmp_path)
    c = LinuxSysfsCollector(hwmon, cpu_root, proc_stat, profile)
    partial = c.sample()
    cpu = partial["cpu"]
    assert isinstance(cpu, dict)
    freqs = cpu["freq_mhz"]
    assert isinstance(freqs, list)
    assert freqs == [3000.0, 3100.0, 3200.0, 3300.0]


def test_sample_collects_storage_and_memory_temps(tmp_path):
    hwmon, cpu_root, proc_stat, profile = _build_fake_sysfs(tmp_path)
    c = LinuxSysfsCollector(hwmon, cpu_root, proc_stat, profile)
    partial = c.sample()
    storage = partial["storage_temps_c"]
    memory = partial["memory_temps_c"]
    assert isinstance(storage, dict)
    assert isinstance(memory, dict)
    assert any(v == 33.0 for v in storage.values())
    assert any(v == 34.0 for v in memory.values())


def test_sample_collects_fans(tmp_path):
    hwmon, cpu_root, proc_stat, profile = _build_fake_sysfs(tmp_path)
    c = LinuxSysfsCollector(hwmon, cpu_root, proc_stat, profile)
    partial = c.sample()
    fans = partial["fans"]
    assert isinstance(fans, list)
    names = sorted(f.name for f in fans)
    rpms = sorted(f.rpm for f in fans if f.rpm is not None)
    assert names == ["cpu_fan", "gpu_fan"]
    assert rpms == [2900, 2950]


def test_sample_collects_platform_state(tmp_path):
    hwmon, cpu_root, proc_stat, profile = _build_fake_sysfs(tmp_path)
    c = LinuxSysfsCollector(hwmon, cpu_root, proc_stat, profile)
    partial = c.sample()
    state = partial["platform_state"]
    assert isinstance(state, dict)
    assert state["governor"] == "performance"
    assert state["epp"] == "performance"
    assert state["platform_profile"] == "performance"
    assert state["boost"] == "1"


def test_skip_acad_and_other_non_thermal_devices(tmp_path):
    hwmon, cpu_root, proc_stat, profile = _build_fake_sysfs(tmp_path)
    c = LinuxSysfsCollector(hwmon, cpu_root, proc_stat, profile)
    partial = c.sample()
    cpu = partial["cpu"]
    assert isinstance(cpu, dict)
    storage = partial["storage_temps_c"]
    memory = partial["memory_temps_c"]
    assert isinstance(storage, dict)
    assert isinstance(memory, dict)
    # 99.999 от ACAD не должен попасть никуда
    assert 99.999 not in cpu["temps_c"].values()  # type: ignore[union-attr]
    assert 99.999 not in storage.values()
    assert 99.999 not in memory.values()


def test_load_pct_first_sample_zero_then_delta(tmp_path):
    hwmon, cpu_root, proc_stat, profile = _build_fake_sysfs(tmp_path)
    c = LinuxSysfsCollector(hwmon, cpu_root, proc_stat, profile)
    first = c.sample()
    cpu = first["cpu"]
    assert isinstance(cpu, dict)
    loads = cpu["load_pct"]
    assert isinstance(loads, list)
    assert loads == [0.0, 0.0, 0.0, 0.0]

    # Move the clock forward — increase active time on all cores
    proc_stat.write_text(
        "\n".join(
            [
                "cpu  20 0 10 100 0 0 0 0 0 0",
                "cpu0 5 0 3 25 0 0 0 0 0 0",
                "cpu1 6 0 3 24 0 0 0 0 0 0",
                "cpu2 5 0 4 26 0 0 0 0 0 0",
                "cpu3 6 0 3 25 0 0 0 0 0 0",
                "",
            ]
        )
    )
    second = c.sample()
    cpu2 = second["cpu"]
    assert isinstance(cpu2, dict)
    loads2 = cpu2["load_pct"]
    assert isinstance(loads2, list)
    assert all(0.0 < L < 100.0 for L in loads2), loads2


def test_iowait_pct_emitted_separately_from_load_pct(tmp_path):
    """iowait не входит в load_pct (thermal-load), но эмиттится отдельным
    сигналом (workload classifier discrimination)."""
    hwmon, cpu_root, proc_stat, profile = _build_fake_sysfs(tmp_path)
    c = LinuxSysfsCollector(hwmon, cpu_root, proc_stat, profile)
    # 1-й sample — baseline
    c.sample()
    # 2-й sample: cpu0 — heavy iowait (idle=20+0→25+10 iowait=10), остальные idle:
    # values per cpu: user nice system idle iowait irq softirq steal guest guest_nice
    proc_stat.write_text(
        "\n".join(
            [
                "cpu  10 0 5 85 10 0 0 0 0 0",
                "cpu0 2 0 1 25 10 0 0 0 0 0",  # +5 idle +10 iowait → iowait_pct высокий
                "cpu1 3 0 1 20 0 0 0 0 0 0",   # no delta
                "cpu2 2 0 2 22 0 0 0 0 0 0",   # marginal
                "cpu3 3 0 1 21 0 0 0 0 0 0",   # marginal
                "",
            ]
        )
    )
    second = c.sample()
    cpu = second["cpu"]
    assert isinstance(cpu, dict)
    iowaits = cpu["iowait_pct"]
    loads = cpu["load_pct"]
    assert isinstance(iowaits, list)
    assert isinstance(loads, list)
    assert len(iowaits) == 4
    # cpu0 имеет iowait → iowait_pct > 0
    assert iowaits[0] > 50.0, f"expected high iowait on cpu0, got {iowaits[0]}"
    # При этом load_pct у cpu0 НЕ должен подскочить из-за iowait —
    # iowait counted as idle for thermal-load metric
    assert loads[0] < 50.0, f"load_pct must not include iowait on cpu0, got {loads[0]}"
    # cpu1..3 без iowait → iowait_pct = 0
    for L in iowaits[1:]:
        assert L == 0.0, f"expected zero iowait on idle cores, got {L}"


def test_cost_records_after_sample(tmp_path):
    hwmon, cpu_root, proc_stat, profile = _build_fake_sysfs(tmp_path)
    c = LinuxSysfsCollector(hwmon, cpu_root, proc_stat, profile)
    assert c.cost().sample_us == 0
    c.sample()
    assert c.cost().sample_us > 0


def test_make_returns_none_for_missing_root(tmp_path, monkeypatch):
    """make() default returns None when /sys/class/hwmon doesn't exist."""
    from coolstep.adapters.collectors import linux_sysfs

    monkeypatch.setattr(
        linux_sysfs.LinuxSysfsCollector,
        "discover",
        lambda self: False,
    )
    assert linux_sysfs.make() is None
