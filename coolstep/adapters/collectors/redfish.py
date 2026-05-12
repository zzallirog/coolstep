"""Redfish BMC collector — enterprise rack servers (Dell/HP/Supermicro/Lenovo).

Redfish is a DMTF JSON API spoken by every modern BMC (iDRAC, iLO, IMM,
SuperDoctor, XClarity).  It exposes chassis thermal/power telemetry without
hwmon — on rack hardware the kernel can't see fan RPM directly because the
BMC owns the fan PWMs.

Configuration is env-driven (no creds in code or settings):
    COOLSTEP_REDFISH_URL=https://10.0.0.1
    COOLSTEP_REDFISH_USER=admin
    COOLSTEP_REDFISH_PASS=<password>
    COOLSTEP_REDFISH_VERIFY_TLS=0    (default 1; set 0 for self-signed BMCs)
    COOLSTEP_REDFISH_CHASSIS=Self    (default "Self" → /redfish/v1/Chassis/Self)
    COOLSTEP_REDFISH_INTERVAL=5      (seconds; floor the BMC request rate)

Implementation uses stdlib (urllib) — no extra deps.  The collector skips
discover if the env URL is unset, so it stays silent on workstations.

Outputs:
- fans      — list[FanMetrics] from chassis Thermal->Fans
- cpu temps — inlet/exhaust under cpu.temps_c["inlet"] / ["exhaust"]
- power     — psu watts in cpu.power_w["psu_total"]
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from base64 import b64encode

from coolstep.adapters.collectors._base import CostTracker, stopwatch
from coolstep.core.schema import Cost, FanMetrics, SignalDescriptor

log = logging.getLogger(__name__)

_ENV_URL = "COOLSTEP_REDFISH_URL"
_ENV_USER = "COOLSTEP_REDFISH_USER"
_ENV_PASS = "COOLSTEP_REDFISH_PASS"
_ENV_VERIFY = "COOLSTEP_REDFISH_VERIFY_TLS"
_ENV_CHASSIS = "COOLSTEP_REDFISH_CHASSIS"
_ENV_INTERVAL = "COOLSTEP_REDFISH_INTERVAL"

_DEFAULT_INTERVAL = 5.0
_REQUEST_TIMEOUT = 3.0


def _basic_auth(user: str, password: str) -> str:
    token = b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


class RedfishCollector:
    name = "redfish"

    def __init__(self) -> None:
        self._cost = CostTracker()
        self._url = os.environ.get(_ENV_URL, "").rstrip("/")
        self._user = os.environ.get(_ENV_USER, "")
        self._pass = os.environ.get(_ENV_PASS, "")
        self._chassis = os.environ.get(_ENV_CHASSIS, "Self")
        self._interval = float(os.environ.get(_ENV_INTERVAL, _DEFAULT_INTERVAL))
        self._verify_tls = os.environ.get(_ENV_VERIFY, "1") not in ("0", "false", "no")
        self._last_fetch_ts: float = 0.0
        self._cached_partial: dict[str, object] = {}

        if self._verify_tls:
            self._ssl_ctx: ssl.SSLContext | None = None  # default verifying ctx
        else:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_ctx = ctx

    def discover(self) -> bool:
        if not (self._url and self._user and self._pass):
            return False
        # Ping the service root — confirms reachability and auth.
        try:
            self._get("/redfish/v1/")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("redfish discover failed at %s: %r", self._url, exc)
            return False

    def cost(self) -> Cost:
        return Cost(sample_us=self._cost.avg_us(), rss_kb=0)

    def signals(self) -> list[SignalDescriptor]:
        return [
            SignalDescriptor(
                name="fans", unit="RPM", dtype="list[FanMetrics]",
                source=f"{self._url}/redfish/v1/Chassis/{self._chassis}/Thermal#Fans",
                cardinality="per-device",
                description="Chassis fan RPM via Redfish Thermal API",
                requires=("BMC with Redfish", "creds in env"),
            ),
            SignalDescriptor(
                name="cpu.temps_c.inlet", unit="°C", dtype="float",
                source=f"{self._url}/redfish/v1/Chassis/{self._chassis}/Thermal#Temperatures",
                cardinality="scalar",
                description="Inlet (cold-aisle) air temperature",
                requires=("BMC with Redfish",),
            ),
            SignalDescriptor(
                name="cpu.power_w.psu", unit="W", dtype="dict[str, float]",
                source=f"{self._url}/redfish/v1/Chassis/{self._chassis}/Power#PowerSupplies",
                cardinality="dict",
                description="PSU output power",
                requires=("BMC with Redfish",),
            ),
        ]

    def sample(self) -> dict[str, object]:
        with stopwatch() as sw:
            now = time.monotonic()
            # Rate-limit BMC requests — they're slow and we don't want to DOS the BMC.
            if now - self._last_fetch_ts < self._interval and self._cached_partial:
                self._cost.record_us(sw.elapsed_us)
                return dict(self._cached_partial)

            partial: dict[str, object] = {}
            try:
                thermal = self._get(f"/redfish/v1/Chassis/{self._chassis}/Thermal")
                power = self._get(f"/redfish/v1/Chassis/{self._chassis}/Power")
            except Exception as exc:  # noqa: BLE001
                log.debug("redfish sample failed: %r", exc)
                self._cost.record_us(sw.elapsed_us)
                return self._cached_partial

            # ---- Fans ----
            fans: list[FanMetrics] = []
            for fan in (thermal or {}).get("Fans", []) or []:
                name = fan.get("Name") or fan.get("FanName") or "fan"
                rpm = fan.get("Reading") or fan.get("ReadingRPM")
                fans.append(FanMetrics(name=str(name), rpm=int(rpm) if rpm else None))
            if fans:
                partial["fans"] = fans

            # ---- Temps (inlet/exhaust) ----
            temps_c: dict[str, float] = {}
            for t in (thermal or {}).get("Temperatures", []) or []:
                name = (t.get("Name") or "").lower()
                val = t.get("ReadingCelsius")
                if val is None:
                    continue
                if "inlet" in name or "intake" in name:
                    temps_c["inlet"] = float(val)
                elif "exhaust" in name or "outlet" in name:
                    temps_c["exhaust"] = float(val)
                elif "cpu" in name:
                    # Take max across multiple CPU sensors
                    cur = temps_c.get("cpu_max", -273.0)
                    temps_c["cpu_max"] = max(cur, float(val))
            if temps_c:
                partial["cpu"] = {"temps_c": temps_c}

            # ---- Power ----
            psu_total: float = 0.0
            psu_count = 0
            for psu in (power or {}).get("PowerSupplies", []) or []:
                w = psu.get("PowerOutputWatts") or psu.get("LastPowerOutputWatts")
                if w:
                    psu_total += float(w)
                    psu_count += 1
            if psu_count:
                merged_cpu = partial.get("cpu", {})
                if not isinstance(merged_cpu, dict):
                    merged_cpu = {}
                merged_cpu.setdefault("power_w", {})["psu_total"] = psu_total
                partial["cpu"] = merged_cpu

            self._last_fetch_ts = now
            self._cached_partial = partial
            self._cost.record_us(sw.elapsed_us)
            return partial

    # ------------------------------------------------------------------
    # HTTP helper

    def _get(self, path: str) -> dict | None:
        req = urllib.request.Request(
            self._url + path,
            headers={
                "Authorization": _basic_auth(self._user, self._pass),
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 (URL is env-supplied)
                req, timeout=_REQUEST_TIMEOUT, context=self._ssl_ctx,
            ) as resp:
                if resp.status != 200:
                    return None
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            log.debug("redfish HTTP %s on %s", exc.code, path)
            return None


def make() -> RedfishCollector | None:
    if not os.environ.get(_ENV_URL):
        return None
    collector = RedfishCollector()
    return collector if collector.discover() else None
