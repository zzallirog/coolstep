"""Tests for IPMI collector — parsing only (subprocess mocked)."""

from __future__ import annotations

from unittest.mock import patch

from coolstep.adapters.collectors.ipmi import IpmiCollector, _parse_sensor_output

_SAMPLE_OUTPUT = """\
CPU1 Temp        | 52.000     | degrees C  | ok    | 0.000     | 0.000     | 0.000     | 95.000    | 97.000    | 99.000
CPU2 Temp        | 48.000     | degrees C  | ok    | 0.000     | 0.000     | 0.000     | 95.000    | 97.000    | 99.000
Inlet Temp       | 22.000     | degrees C  | ok    | 0.000     | 0.000     | 0.000     | 38.000    | 42.000    | 47.000
Exhaust Temp     | 32.000     | degrees C  | ok    | 0.000     | 0.000     | 0.000     | 60.000    | 65.000    | 70.000
Fan1A            | 4800.000   | RPM        | ok    | 0.000     | 600.000   | 1200.000  | na        | na        | na
Fan1B            | 4500.000   | RPM        | ok    | 0.000     | 600.000   | 1200.000  | na        | na        | na
Fan2A            | 4600.000   | RPM        | ok    | 0.000     | 600.000   | 1200.000  | na        | na        | na
PS1 PG Pwr       | 145.000    | Watts      | ok    | na        | na        | na        | na        | na        | na
PS2 PG Pwr       | 138.000    | Watts      | ok    | na        | na        | na        | na        | na        | na
Voltage 12V      | 11.880     | Volts      | ok    | 11.040    | 11.220    | 11.380    | 12.610    | 12.800    | 13.000
"""


class TestParseSensorOutput:
    def test_fans_extracted(self):
        partial = _parse_sensor_output(_SAMPLE_OUTPUT)
        assert "fans" in partial
        fan_names = [f.name for f in partial["fans"]]
        assert "Fan1A" in fan_names
        assert "Fan1B" in fan_names
        assert "Fan2A" in fan_names
        fan1a = next(f for f in partial["fans"] if f.name == "Fan1A")
        assert fan1a.rpm == 4800

    def test_inlet_exhaust_temps(self):
        partial = _parse_sensor_output(_SAMPLE_OUTPUT)
        temps = partial["cpu"]["temps_c"]
        assert temps["inlet"] == 22.0
        assert temps["exhaust"] == 32.0

    def test_cpu_max_aggregated(self):
        partial = _parse_sensor_output(_SAMPLE_OUTPUT)
        temps = partial["cpu"]["temps_c"]
        # CPU1 (52) and CPU2 (48) → max = 52
        assert temps["cpu_max"] == 52.0

    def test_psu_total(self):
        partial = _parse_sensor_output(_SAMPLE_OUTPUT)
        psu = partial["cpu"]["power_w"]
        assert psu["psu_total"] == 145.0 + 138.0

    def test_unparseable_values_skipped(self):
        bad = "Fan1A | na | RPM | nc | na | na | na | na | na | na\n"
        partial = _parse_sensor_output(bad)
        # "na" -> not parseable as float; no fan emitted
        assert "fans" not in partial


class TestIpmiCollector:
    def test_discover_no_ipmitool(self):
        with patch("coolstep.adapters.collectors.ipmi.shutil.which", return_value=None):
            c = IpmiCollector()
            assert c.discover() is False

    def test_discover_no_kcs_no_host(self, monkeypatch):
        monkeypatch.delenv("COOLSTEP_IPMI_HOST", raising=False)
        with (
            patch("coolstep.adapters.collectors.ipmi.shutil.which", return_value="/usr/bin/ipmitool"),
            patch("os.path.exists", return_value=False),
        ):
            c = IpmiCollector()
            assert c.discover() is False
