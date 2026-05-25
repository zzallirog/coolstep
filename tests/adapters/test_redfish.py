"""Tests for Redfish collector — HTTP mocked via _get override."""

from __future__ import annotations

from unittest.mock import patch

from coolstep.adapters.collectors.redfish import RedfishCollector

_THERMAL_PAYLOAD = {
    "Fans": [
        {"Name": "Fan1", "Reading": 5400},
        {"Name": "Fan2", "Reading": 5200},
    ],
    "Temperatures": [
        {"Name": "Inlet Temp", "ReadingCelsius": 22},
        {"Name": "Exhaust Temp", "ReadingCelsius": 35},
        {"Name": "CPU1 Temp", "ReadingCelsius": 58},
        {"Name": "CPU2 Temp", "ReadingCelsius": 53},
    ],
}

_POWER_PAYLOAD = {
    "PowerSupplies": [
        {"Name": "PS1", "PowerOutputWatts": 152},
        {"Name": "PS2", "PowerOutputWatts": 148},
    ],
}


def _make_collector(monkeypatch) -> RedfishCollector:
    monkeypatch.setenv("COOLSTEP_REDFISH_URL", "https://bmc.example.com")
    monkeypatch.setenv("COOLSTEP_REDFISH_USER", "admin")
    monkeypatch.setenv("COOLSTEP_REDFISH_PASS", "secret")
    monkeypatch.setenv("COOLSTEP_REDFISH_INTERVAL", "0.1")
    monkeypatch.setenv("COOLSTEP_REDFISH_VERIFY_TLS", "0")
    return RedfishCollector()


class TestRedfishCollector:
    def test_no_env_means_no_discover(self, monkeypatch):
        monkeypatch.delenv("COOLSTEP_REDFISH_URL", raising=False)
        c = RedfishCollector()
        assert c.discover() is False

    def test_sample_parses_thermal_and_power(self, monkeypatch):
        c = _make_collector(monkeypatch)

        def fake_get(path: str):
            if path.endswith("/Thermal"):
                return _THERMAL_PAYLOAD
            if path.endswith("/Power"):
                return _POWER_PAYLOAD
            return {}

        with patch.object(c, "_get", side_effect=fake_get):
            partial = c.sample()

        # Fans
        assert "fans" in partial
        names = [f.name for f in partial["fans"]]
        assert "Fan1" in names
        assert "Fan2" in names

        # Temps
        temps = partial["cpu"]["temps_c"]
        assert temps["inlet"] == 22.0
        assert temps["exhaust"] == 35.0
        assert temps["cpu_max"] == 58.0  # max of 58, 53

        # Power
        psu = partial["cpu"]["power_w"]
        assert psu["psu_total"] == 300.0

    def test_rate_limit_caches_partial(self, monkeypatch):
        c = _make_collector(monkeypatch)
        call_count = {"n": 0}

        def fake_get(path: str):
            call_count["n"] += 1
            if path.endswith("/Thermal"):
                return _THERMAL_PAYLOAD
            return _POWER_PAYLOAD

        with patch.object(c, "_get", side_effect=fake_get):
            c.sample()
            initial_calls = call_count["n"]
            # Second sample within interval window → cache hit, no new HTTP calls
            c.sample()
            assert call_count["n"] == initial_calls

    def test_signals_manifest(self, monkeypatch):
        c = _make_collector(monkeypatch)
        sigs = c.signals()
        assert any(s.name == "fans" for s in sigs)
        assert any("inlet" in s.name for s in sigs)
