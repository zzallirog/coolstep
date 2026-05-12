"""Tests for intel_i915 collector — Intel iGPU via i915/xe."""

from __future__ import annotations

from pathlib import Path

import pytest

from coolstep.adapters.collectors.intel_i915 import IntelI915Collector, _intel_cards


def _make_intel_card(drm_root: Path, card_name: str = "card0",
                    freq_mhz: int = 1450,
                    temp_mC: int = 52000,
                    power_uW: int = 8_500_000) -> Path:
    """Build a fake /sys/class/drm/cardN tree for Intel iGPU."""
    card = drm_root / card_name
    (card / "device").mkdir(parents=True)
    (card / "device" / "vendor").write_text("0x8086")

    # New gt/gt0/ layout (i915 4.16+ and xe)
    gt0 = card / "gt" / "gt0"
    gt0.mkdir(parents=True)
    (gt0 / "rps_act_freq_mhz").write_text(str(freq_mhz))
    (gt0 / "throttle_reason_status").write_text("0")
    (gt0 / "throttle_reason_pl1").write_text("0")
    (gt0 / "throttle_reason_pl2").write_text("0")

    # hwmon under device/
    hwmon = card / "device" / "hwmon" / "hwmon99"
    hwmon.mkdir(parents=True)
    (hwmon / "temp1_input").write_text(str(temp_mC))
    (hwmon / "power1_input").write_text(str(power_uW))
    return card


class TestIntelCards:
    def test_only_intel_picked(self, tmp_path: Path):
        # Mix Intel + AMD cards
        _make_intel_card(tmp_path, "card0")
        amd_card = tmp_path / "card1"
        (amd_card / "device").mkdir(parents=True)
        (amd_card / "device" / "vendor").write_text("0x1002")

        cards = _intel_cards(tmp_path)
        assert len(cards) == 1
        assert cards[0].name == "card0"

    def test_empty_root(self, tmp_path: Path):
        assert _intel_cards(tmp_path / "nonexistent") == []


class TestIntelI915Collector:
    def test_discover_no_intel(self, tmp_path: Path):
        c = IntelI915Collector(drm_root=tmp_path)
        assert c.discover() is False

    def test_discover_with_intel(self, tmp_path: Path):
        _make_intel_card(tmp_path, "card0")
        c = IntelI915Collector(drm_root=tmp_path)
        assert c.discover() is True

    def test_sample_reads_freq_temp_power(self, tmp_path: Path):
        _make_intel_card(tmp_path, "card0", freq_mhz=1300, temp_mC=58000, power_uW=12_000_000)
        c = IntelI915Collector(drm_root=tmp_path)
        c.discover()
        partial = c.sample()
        assert "gpus" in partial
        gpu = partial["gpus"][0]
        assert gpu.name == "intel_card0"
        assert gpu.sclk_mhz == 1300.0
        assert gpu.temp_c == 58.0
        assert gpu.power_w == 12.0

    def test_throttle_reasons_surfaced(self, tmp_path: Path):
        card = _make_intel_card(tmp_path, "card0")
        # Flip one throttle reason to active
        (card / "gt" / "gt0" / "throttle_reason_pl1").write_text("1")
        c = IntelI915Collector(drm_root=tmp_path)
        c.discover()
        partial = c.sample()
        ps = partial.get("platform_state", {})
        assert any("pl1" in k and v == "1" for k, v in ps.items())

    def test_multiple_intel_cards(self, tmp_path: Path):
        _make_intel_card(tmp_path, "card0", freq_mhz=1100)
        _make_intel_card(tmp_path, "card1", freq_mhz=1700)
        c = IntelI915Collector(drm_root=tmp_path)
        c.discover()
        partial = c.sample()
        assert len(partial["gpus"]) == 2

    def test_signals_manifest_complete(self, tmp_path: Path):
        _make_intel_card(tmp_path, "card0")
        c = IntelI915Collector(drm_root=tmp_path)
        c.discover()
        sig_names = {s.name for s in c.signals()}
        assert {"gpu.temp_c", "gpu.sclk_mhz", "gpu.power_w", "gpu.throttle_reasons"} <= sig_names
