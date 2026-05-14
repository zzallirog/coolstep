"""IPMI fallback collector — older rack servers without Redfish.

Uses `ipmitool sensor` subprocess (read-only). Slower than Redfish (a single
`ipmitool sensor` call can take 1-3s on cold BMCs), so we rate-limit and cache.

Configuration is env-driven:
    COOLSTEP_IPMI_HOST=10.0.0.1       # required (else BMC-local mode via KCS)
    COOLSTEP_IPMI_USER=ADMIN
    COOLSTEP_IPMI_PASS=<password>
    COOLSTEP_IPMI_INTERVAL=10         # default seconds between ipmitool calls

When no IPMI_HOST is set, the collector tries local mode (KCS interface via
`/dev/ipmi*`).  Useful when the server runs coolstep on its own host OS and
has the openipmi kernel driver loaded.

Output:
- fans      — list[FanMetrics] from "Fan*" sensors
- cpu temps — temps_c with sensor names from "CPU*"/"Inlet"/"Exhaust"
- psu power — power_w["psu_total"] from "PS*" / "Pwr" sensors
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time

from coolstep.adapters.collectors._base import CostTracker, stopwatch
from coolstep.core.schema import Cost, FanMetrics, SignalDescriptor

log = logging.getLogger(__name__)

_ENV_HOST = "COOLSTEP_IPMI_HOST"
_ENV_USER = "COOLSTEP_IPMI_USER"
_ENV_PASS = "COOLSTEP_IPMI_PASS"
_ENV_INTERVAL = "COOLSTEP_IPMI_INTERVAL"

_DEFAULT_INTERVAL = 10.0
_SUBPROC_TIMEOUT = 8.0

# `ipmitool sensor` output is `NAME | VALUE | UNIT | STATUS | LNR | LCR | LNC | UNC | UCR | UNR`
_SENSOR_LINE = re.compile(r"^([^|]+)\s*\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|")


class IpmiCollector:
    name = "ipmi"

    def __init__(self) -> None:
        self._cost = CostTracker()
        self._host = os.environ.get(_ENV_HOST, "")
        self._user = os.environ.get(_ENV_USER, "")
        self._pass = os.environ.get(_ENV_PASS, "")
        self._interval = float(os.environ.get(_ENV_INTERVAL, _DEFAULT_INTERVAL))
        self._last_fetch_ts: float = 0.0
        self._cached_partial: dict[str, object] = {}
        self._cmd_base: list[str] = []

    def discover(self) -> bool:
        if not shutil.which("ipmitool"):
            return False

        if self._host:
            # Remote mode requires creds
            if not (self._user and self._pass):
                return False
            self._cmd_base = [
                "ipmitool", "-I", "lanplus",
                "-H", self._host,
                "-U", self._user,
                "-P", self._pass,
            ]
        else:
            # Local KCS — needs /dev/ipmi* (openipmi driver)
            if not (os.path.exists("/dev/ipmi0") or os.path.exists("/dev/ipmidev/0")):
                return False
            self._cmd_base = ["ipmitool"]

        # Probe with `mc info` — confirms connectivity before first sample
        try:
            r = subprocess.run(
                self._cmd_base + ["mc", "info"],
                capture_output=True, text=True, timeout=_SUBPROC_TIMEOUT,
            )
            return r.returncode == 0
        except Exception as exc:  # noqa: BLE001
            log.warning("ipmi discover failed: %r", exc)
            return False

    def cost(self) -> Cost:
        return Cost(sample_us=self._cost.avg_us(), rss_kb=0)

    def signals(self) -> list[SignalDescriptor]:
        return [
            SignalDescriptor(
                name="fans", unit="RPM", dtype="list[FanMetrics]",
                source="ipmitool sensor (Fan* rows)",
                cardinality="per-device",
                description="Server fan RPMs via IPMI SDR",
                requires=("ipmitool", "BMC reachable",),
            ),
            SignalDescriptor(
                name="cpu.temps_c", unit="°C", dtype="dict[str, float]",
                source="ipmitool sensor (Temperature rows)",
                cardinality="dict",
                description="CPU / inlet / exhaust temps via IPMI SDR",
                requires=("ipmitool",),
            ),
        ]

    def sample(self) -> dict[str, object]:
        with stopwatch() as sw:
            now = time.monotonic()
            if now - self._last_fetch_ts < self._interval and self._cached_partial:
                self._cost.record_us(sw.elapsed_us)
                return dict(self._cached_partial)

            try:
                r = subprocess.run(
                    self._cmd_base + ["sensor"],
                    capture_output=True, text=True, timeout=_SUBPROC_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("ipmi sample failed: %r", exc)
                self._cost.record_us(sw.elapsed_us)
                return self._cached_partial

            if r.returncode != 0:
                self._cost.record_us(sw.elapsed_us)
                return self._cached_partial

            partial = _parse_sensor_output(r.stdout)
            self._last_fetch_ts = now
            self._cached_partial = partial
            self._cost.record_us(sw.elapsed_us)
            return partial


def _parse_sensor_output(text: str) -> dict[str, object]:
    fans: list[FanMetrics] = []
    temps_c: dict[str, float] = {}
    psu_total = 0.0
    psu_count = 0

    for line in text.splitlines():
        m = _SENSOR_LINE.match(line)
        if not m:
            continue
        name = m.group(1).strip()
        value_raw = m.group(2).strip()
        unit = m.group(3).strip().lower()

        # Try to parse numeric; "na" or "disabled" → skip
        try:
            value = float(value_raw)
        except ValueError:
            continue

        n_low = name.lower()
        if "rpm" in unit and ("fan" in n_low or n_low.startswith("fan")):
            fans.append(FanMetrics(name=name, rpm=int(value)))
        elif "degrees c" in unit or "celsius" in unit:
            key = "cpu"
            if "inlet" in n_low or "intake" in n_low or "ambient" in n_low:
                key = "inlet"
            elif "exhaust" in n_low or "outlet" in n_low:
                key = "exhaust"
            elif "cpu" in n_low:
                key = "cpu_max"
                cur = temps_c.get(key, -273.0)
                temps_c[key] = max(cur, value)
                continue
            else:
                key = n_low.replace(" ", "_")[:32]
            temps_c[key] = value
        elif "watts" in unit and ("ps" in n_low or "psu" in n_low or "pwr" in n_low):
            psu_total += value
            psu_count += 1

    partial: dict[str, object] = {}
    if fans:
        partial["fans"] = fans
    if temps_c or psu_count:
        cpu_dict: dict[str, object] = {}
        if temps_c:
            cpu_dict["temps_c"] = temps_c
        if psu_count:
            cpu_dict["power_w"] = {"psu_total": psu_total}
        partial["cpu"] = cpu_dict
    return partial


def make() -> IpmiCollector | None:
    if not shutil.which("ipmitool"):
        return None
    collector = IpmiCollector()
    return collector if collector.discover() else None
