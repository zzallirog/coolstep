"""Tests for arm_thermal collector — ACPI / generic thermal_zone."""

from __future__ import annotations

from pathlib import Path

from coolstep.adapters.collectors.arm_thermal import ArmThermalCollector


def _setup_zones(root: Path, zones: list[tuple[str, int]]) -> None:
    """Build /sys/class/thermal/thermal_zoneN entries.

    zones = list of (zone_type, temp_milli_c) tuples.
    """
    for idx, (ztype, temp) in enumerate(zones):
        zd = root / f"thermal_zone{idx}"
        zd.mkdir(parents=True, exist_ok=True)
        (zd / "type").write_text(ztype)
        (zd / "temp").write_text(str(temp))


class TestArmThermalCollector:
    def test_no_thermal_dir(self, tmp_path: Path):
        c = ArmThermalCollector(root=tmp_path / "nonexistent")
        assert c.discover() is False

    def test_empty_thermal_dir(self, tmp_path: Path):
        c = ArmThermalCollector(root=tmp_path)
        assert c.discover() is False

    def test_single_zone(self, tmp_path: Path):
        _setup_zones(tmp_path, [("cpu-thermal", 45_000)])
        c = ArmThermalCollector(root=tmp_path)
        assert c.discover() is True
        partial = c.sample()
        assert partial["cpu"]["temps_c"]["cpu-thermal"] == 45.0

    def test_multiple_zones(self, tmp_path: Path):
        _setup_zones(tmp_path, [
            ("x86_pkg_temp", 67_500),
            ("acpitz", 38_000),
            ("nvme-pci-0100", 41_000),
        ])
        c = ArmThermalCollector(root=tmp_path)
        c.discover()
        partial = c.sample()
        temps = partial["cpu"]["temps_c"]
        assert temps["x86_pkg_temp"] == 67.5
        assert temps["acpitz"] == 38.0

    def test_duplicate_types_get_suffix(self, tmp_path: Path):
        _setup_zones(tmp_path, [
            ("cpu-thermal", 45_000),
            ("cpu-thermal", 46_000),  # duplicate type → suffixed
        ])
        c = ArmThermalCollector(root=tmp_path)
        c.discover()
        partial = c.sample()
        temps = partial["cpu"]["temps_c"]
        assert len(temps) == 2

    def test_implausible_reading_filtered(self, tmp_path: Path):
        _setup_zones(tmp_path, [("cpu-thermal", -1), ("acpitz", 38_000)])
        c = ArmThermalCollector(root=tmp_path)
        c.discover()
        partial = c.sample()
        temps = partial["cpu"]["temps_c"]
        # -1 (kernel placeholder for "no reading") filtered
        assert "acpitz" in temps
        assert "cpu-thermal" not in temps
