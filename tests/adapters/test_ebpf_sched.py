"""Tests for ebpf_sched collector — JSON parser logic."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from coolstep.adapters.collectors.ebpf_sched import EbpfSchedCollector, _can_run_bpf


class TestConsumeMap:
    def test_per_cpu_dict(self):
        c = EbpfSchedCollector()
        c._consume_map({"@switches": {"0": 1234, "1": 2345, "3": 800}})
        assert "switches" in c._latest
        switches = c._latest["switches"]
        assert isinstance(switches, list)
        assert switches[0] == 1234.0
        assert switches[1] == 2345.0
        assert switches[3] == 800.0
        # CPU 2 had no events → gap should be 0.0
        assert switches[2] == 0.0

    def test_scalar(self):
        c = EbpfSchedCollector()
        c._consume_map({"@forks": 42})
        assert c._latest["forks"] == 42.0

    def test_multiple_maps_merged(self):
        c = EbpfSchedCollector()
        c._consume_map({"@switches": {"0": 100, "1": 200}})
        c._consume_map({"@forks": 5})
        c._consume_map({"@execs": 3})
        assert "switches" in c._latest
        assert "forks" in c._latest
        assert "execs" in c._latest

    def test_empty_map_ignored(self):
        c = EbpfSchedCollector()
        c._consume_map({"@switches": {}})
        # Empty dict → no key written
        assert "switches" not in c._latest


class TestSample:
    def test_sample_empty_when_no_data(self):
        c = EbpfSchedCollector()
        assert c.sample() == {}

    def test_sample_renders_ctx_switches(self):
        c = EbpfSchedCollector()
        c._consume_map({"@switches": {"0": 5000, "1": 3000, "2": 1000}})
        partial = c.sample()
        ps = partial["platform_state"]
        assert ps["ebpf.ctx_switch_max"] == "5000"
        assert ps["ebpf.ctx_switch_total"] == "9000"

    def test_sample_renders_forks(self):
        c = EbpfSchedCollector()
        c._consume_map({"@forks": 12})
        partial = c.sample()
        assert partial["platform_state"]["ebpf.fork_rate"] == "12"

    def test_signals_complete(self):
        c = EbpfSchedCollector()
        sig_names = {s.name for s in c.signals()}
        assert "cpu.ctx_switch_rate" in sig_names
        assert "cpu.fork_rate" in sig_names
        assert "cpu.exec_rate" in sig_names


class TestDiscover:
    def test_no_bpftrace_binary(self, monkeypatch):
        monkeypatch.setattr(
            "coolstep.adapters.collectors.ebpf_sched.shutil.which",
            lambda _: None,
        )
        c = EbpfSchedCollector()
        assert c.discover() is False

    def test_close_no_proc_safe(self):
        c = EbpfSchedCollector()
        c.close()  # no exception
