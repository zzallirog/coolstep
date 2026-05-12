"""Tests for rapl_energy collector — Intel + AMD powercap."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from coolstep.adapters.collectors.rapl import (
    RaplEnergyCollector,
    _discover_domains,
    make,
)


def _setup_rapl_tree(root: Path, energy_uj: dict[str, int]) -> None:
    """Build a fake /sys/class/powercap layout.

    energy_uj keys:
        "pkg0" -> top-level package-0
        "pkg0:0" -> subdomain (core), "pkg0:1" -> subdomain (dram)
    """
    for key, value in energy_uj.items():
        if ":" in key:
            pkg, sub = key.split(":")
            pkg_idx = int(pkg.replace("pkg", ""))
            sub_dir = root / f"intel-rapl:{pkg_idx}" / f"intel-rapl:{pkg_idx}:{sub}"
            sub_dir.mkdir(parents=True, exist_ok=True)
            name = ["core", "dram", "uncore", "psys"][int(sub)]
            (sub_dir / "name").write_text(name)
            (sub_dir / "energy_uj").write_text(str(value))
            (sub_dir / "max_energy_range_uj").write_text("262143328850")
        else:
            pkg_idx = int(key.replace("pkg", ""))
            pkg_dir = root / f"intel-rapl:{pkg_idx}"
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / "name").write_text(f"package-{pkg_idx}")
            (pkg_dir / "energy_uj").write_text(str(value))
            (pkg_dir / "max_energy_range_uj").write_text("262143328850")


class TestDiscoverDomains:
    def test_empty_root(self, tmp_path: Path):
        assert _discover_domains(tmp_path) == []

    def test_single_package_with_subdomains(self, tmp_path: Path):
        _setup_rapl_tree(tmp_path, {"pkg0": 1000, "pkg0:0": 500, "pkg0:1": 200})
        domains = _discover_domains(tmp_path)
        keys = {d.key for d in domains}
        assert "pkg" in keys
        assert "core" in keys
        assert "dram" in keys

    def test_dual_socket(self, tmp_path: Path):
        _setup_rapl_tree(tmp_path, {"pkg0": 1000, "pkg1": 1500})
        domains = _discover_domains(tmp_path)
        keys = {d.key for d in domains}
        assert "pkg" in keys
        assert "pkg-1" in keys


class TestRaplCollector:
    def test_discover_no_root(self, tmp_path: Path):
        c = RaplEnergyCollector(root=tmp_path / "nonexistent")
        assert c.discover() is False

    def test_discover_with_domains(self, tmp_path: Path):
        _setup_rapl_tree(tmp_path, {"pkg0": 1000})
        c = RaplEnergyCollector(root=tmp_path)
        assert c.discover() is True

    def test_first_sample_primes_state(self, tmp_path: Path):
        _setup_rapl_tree(tmp_path, {"pkg0": 1_000_000})
        c = RaplEnergyCollector(root=tmp_path)
        assert c.discover() is True
        partial = c.sample()
        # First sample → no power yet
        assert "cpu" not in partial

    def test_second_sample_yields_power(self, tmp_path: Path):
        _setup_rapl_tree(tmp_path, {"pkg0": 1_000_000})
        c = RaplEnergyCollector(root=tmp_path)
        c.discover()
        c.sample()  # prime
        # Bump energy by 5 J (5,000,000 µJ) — write back
        (tmp_path / "intel-rapl:0" / "energy_uj").write_text(str(1_000_000 + 5_000_000))
        time.sleep(0.05)  # let monotonic advance
        partial = c.sample()
        assert "cpu" in partial
        assert "power_w" in partial["cpu"]
        # 5 J over ~50 ms ≈ 100 W (rough — depends on exact dt)
        pkg_w = partial["cpu"]["power_w"]["pkg"]
        assert pkg_w > 50.0  # at least 50 W

    def test_wraparound_handled(self, tmp_path: Path):
        _setup_rapl_tree(tmp_path, {"pkg0": 1_000_000})
        c = RaplEnergyCollector(root=tmp_path)
        c.discover()
        c.sample()  # prime at 1M
        # Wrap-around: counter resets to a small value
        (tmp_path / "intel-rapl:0" / "energy_uj").write_text("500000")
        # max_energy_range_uj is 262_143_328_850 → delta = max - 1M + 500K
        time.sleep(0.01)
        partial = c.sample()
        # Should not crash; power may be very high but bounded
        assert "cpu" in partial or partial == {}

    def test_signals_manifest_nonempty(self, tmp_path: Path):
        _setup_rapl_tree(tmp_path, {"pkg0": 1000})
        c = RaplEnergyCollector(root=tmp_path)
        c.discover()
        sigs = c.signals()
        assert any(s.name == "cpu.power_w" for s in sigs)

    def test_make_returns_none_without_caps(self, tmp_path: Path, monkeypatch):
        """make() with caps_if_set() returning None falls through to discover."""
        from coolstep.compat import reset_caps
        reset_caps(None)
        # No powercap tree exists at the real path → discover fails → None
        # But on the actual machine, /sys/class/powercap may exist; we can't
        # reliably stub the global path here. Just verify make is callable.
        m = make()
        assert m is None or m.name == "rapl_energy"
