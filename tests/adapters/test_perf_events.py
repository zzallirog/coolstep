"""Tests for perf_events collector — subprocess mocked."""

from __future__ import annotations

import pytest

from coolstep.adapters.collectors.perf_events import PerfEventsCollector

_SAMPLE_PERF_LINES = [
    # ts, value, unit, event, run_ns, run_pct
    "1.000132546,1500000000,,cycles,1000128126,100.00",
    "1.000132546,1200000000,,instructions,1000128126,100.00",
    "1.000132546,5000000,,cache-misses,1000128126,100.00",
    "1.000132546,100000000,,cache-references,1000128126,100.00",
    "1.000132546,800.5,,task-clock,1000128126,100.00",
    "2.000300000,1600000000,,cycles,1000128126,100.00",
    "2.000300000,1300000000,,instructions,1000128126,100.00",
    "2.000300000,6000000,,cache-misses,1000128126,100.00",
    "2.000300000,110000000,,cache-references,1000128126,100.00",
    "2.000300000,850.0,,task-clock,1000128126,100.00",
]


class TestPerfReaderParser:
    def test_publish_computes_derived(self):
        """Direct _publish() call — bypasses subprocess."""
        c = PerfEventsCollector()
        c._publish({
            "cycles": 1_500_000_000,
            "instructions": 1_200_000_000,
            "cache-misses": 5_000_000,
            "cache-references": 100_000_000,
            "task-clock": 800.0,
        })
        latest = c._latest
        # CPI = cycles / instructions
        assert latest["pmu.cpi"] == pytest.approx(1.25, rel=0.01)
        # cache_miss_pct = 5 / 100 = 5%
        assert latest["pmu.cache_miss_pct"] == pytest.approx(5.0, rel=0.01)
        # Rates: 1.5B events / 1s = 1.5B/s
        assert latest["pmu.cycles"] == pytest.approx(1.5e9, rel=0.01)

    def test_publish_handles_zero_instructions(self):
        c = PerfEventsCollector()
        c._publish({"cycles": 1000, "instructions": 0})
        # No division by zero
        assert "pmu.cpi" not in c._latest

    def test_publish_handles_missing_refs(self):
        c = PerfEventsCollector()
        c._publish({"cache-misses": 5000})  # no cache-references → no miss_pct
        assert "pmu.cache_miss_pct" not in c._latest


class TestPerfDiscover:
    def test_no_perf_binary(self, monkeypatch):
        monkeypatch.setattr(
            "coolstep.adapters.collectors.perf_events.shutil.which",
            lambda _: None,
        )
        c = PerfEventsCollector()
        assert c.discover() is False

    def test_paranoid_too_high(self, tmp_path, monkeypatch):
        # Force paranoid=3
        f = tmp_path / "paranoid"
        f.write_text("3\n")
        monkeypatch.setattr(
            "coolstep.adapters.collectors.perf_events._PARANOID_FILE", f,
        )
        c = PerfEventsCollector()
        assert c.discover() is False

    def test_paranoid_unknown_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "coolstep.adapters.collectors.perf_events._PARANOID_FILE",
            tmp_path / "nonexistent",
        )
        c = PerfEventsCollector()
        assert c.discover() is False


class TestPerfSample:
    def test_sample_empty_until_published(self):
        c = PerfEventsCollector()
        partial = c.sample()
        assert partial == {}

    def test_sample_after_publish(self):
        c = PerfEventsCollector()
        c._publish({"cycles": 1e9, "instructions": 1e9})
        partial = c.sample()
        assert "platform_state" in partial
        keys = partial["platform_state"]
        assert any("pmu.cycles" in k for k in keys)
        assert any("pmu.cpi" in k for k in keys)

    def test_signals_have_required_pmu_events(self):
        c = PerfEventsCollector()
        sig_names = {s.name for s in c.signals()}
        assert "cpu.pmu.cycles" in sig_names
        assert "cpu.pmu.cpi" in sig_names
        assert "cpu.pmu.cache_miss_pct" in sig_names

    def test_close_no_proc(self):
        """close() must be safe without active subprocess."""
        c = PerfEventsCollector()
        c.close()  # should not raise
