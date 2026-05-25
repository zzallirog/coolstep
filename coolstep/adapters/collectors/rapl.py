"""RAPL energy collector — Intel & AMD via powercap interface.

Reads `/sys/class/powercap/intel-rapl/*/energy_uj` (microjoules counter).
Both Intel and modern AMD platforms expose RAPL through the same powercap
interface — the path name `intel-rapl` is historical; AMD kernels reuse it.

Layout:
    intel-rapl/
    ├── intel-rapl:0/                    package 0
    │   ├── name              -> "package-0"
    │   ├── energy_uj         -> counter (µJ)
    │   ├── max_energy_range_uj
    │   └── intel-rapl:0:0/              subdomain (cores / dram / uncore / psys)
    │       ├── name          -> "core" | "dram" | "uncore" | "psys"
    │       └── energy_uj

Power is derived as `Δenergy_uj / Δt`.  First sample primes the state and
returns no power values; subsequent samples produce deltas.

Permission caveat — post-2020 kernels (CVE-2020-8694 mitigation) restrict
`energy_uj` reads to root.  If the file is unreadable, the collector
records the limitation in `discover()` warnings and still registers (so
the dashboard shows the gap) but yields no values.

Output → fills `cpu.power_w` dict with keys:
    pkg / pkg-0 / pkg-1   - package(s)
    dram / dram-0         - DRAM
    cores / cores-0       - core domain
    uncore / uncore-0     - uncore
    psys                  - platform-wide (rare)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from coolstep.adapters.collectors._base import CostTracker, stopwatch
from coolstep.core.schema import Cost, SignalDescriptor

log = logging.getLogger(__name__)

_POWERCAP_ROOT = Path("/sys/class/powercap")


@dataclass
class _Domain:
    """One RAPL energy domain (e.g., package-0:core)."""

    key: str          # human-readable: "pkg", "pkg-1", "dram", "cores-0", etc.
    energy_path: Path # /sys/class/powercap/.../energy_uj
    max_uj: int       # max_energy_range_uj for wrap-around handling


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except OSError:
        return None


def _discover_domains(root: Path) -> list[_Domain]:
    """Walk powercap tree and return all known energy domains."""
    domains: list[_Domain] = []
    if not root.is_dir():
        return domains

    pkg_index = 0
    for pkg_dir in sorted(root.iterdir()):
        # Top-level packages: intel-rapl:0, intel-rapl:1, ...
        if not pkg_dir.name.startswith("intel-rapl:") or pkg_dir.name.count(":") != 1:
            continue
        (pkg_dir / "name").read_text().strip() if (pkg_dir / "name").exists() else f"package-{pkg_index}"
        energy = pkg_dir / "energy_uj"
        max_uj_p = pkg_dir / "max_energy_range_uj"
        if energy.exists():
            max_uj = _read_int(max_uj_p) or 0
            key = "pkg" if pkg_index == 0 else f"pkg-{pkg_index}"
            domains.append(_Domain(key, energy, max_uj))

        # Subdomains: intel-rapl:0:0, intel-rapl:0:1, ...
        for sub in sorted(pkg_dir.iterdir()):
            if not sub.name.startswith("intel-rapl:") or sub.name.count(":") != 2:
                continue
            sub_name = (sub / "name").read_text().strip() if (sub / "name").exists() else ""
            sub_energy = sub / "energy_uj"
            if not sub_energy.exists() or not sub_name:
                continue
            sub_max = _read_int(sub / "max_energy_range_uj") or 0
            # Map kernel names to clean keys; multi-package -> suffix
            base = sub_name.lower()  # "core", "dram", "uncore", "psys"
            key = base if pkg_index == 0 else f"{base}-{pkg_index}"
            domains.append(_Domain(key, sub_energy, sub_max))

        pkg_index += 1

    return domains


class RaplEnergyCollector:
    name = "rapl_energy"

    def __init__(self, root: Path = _POWERCAP_ROOT) -> None:
        self._root = root
        self._cost = CostTracker()
        self._domains: list[_Domain] = []
        self._last_energy: dict[str, int] = {}
        self._last_ts: float = 0.0
        self._readable: bool = False

    def discover(self) -> bool:
        self._domains = _discover_domains(self._root)
        if not self._domains:
            return False
        # Probe readability — if all are blocked, still register but mark unreadable
        for d in self._domains:
            try:
                d.energy_path.read_text()
                self._readable = True
                break
            except PermissionError:
                continue
            except OSError:
                continue
        if not self._readable:
            log.warning(
                "rapl: %d energy domains found but unreadable (CVE-2020-8694 "
                "mitigation — run as root or `sudo setcap cap_sys_admin+ep`)",
                len(self._domains),
            )
        return bool(self._domains)

    def cost(self) -> Cost:
        return Cost(sample_us=self._cost.avg_us(), rss_kb=0)

    def signals(self) -> list[SignalDescriptor]:
        return [
            SignalDescriptor(
                name="cpu.power_w",
                unit="W",
                dtype="dict[str, float]",
                source="/sys/class/powercap/intel-rapl/*/energy_uj",
                cardinality="dict",
                description="RAPL package + subdomain power, delta-derived",
                requires=("powercap kernel feature", "CAP_SYS_ADMIN for energy_uj"),
            ),
        ]

    def sample(self) -> dict[str, object]:
        with stopwatch() as sw:
            partial: dict[str, object] = {}
            if not self._readable:
                self._cost.record_us(sw.elapsed_us)
                return partial

            now = time.monotonic()
            energies: dict[str, int] = {}
            for d in self._domains:
                val = _read_int(d.energy_path)
                if val is not None:
                    energies[d.key] = val

            if not self._last_energy:
                # First read primes state — no delta yet
                self._last_energy = energies
                self._last_ts = now
                self._cost.record_us(sw.elapsed_us)
                return partial

            dt = now - self._last_ts
            if dt <= 0.0:
                self._cost.record_us(sw.elapsed_us)
                return partial

            power_w: dict[str, float] = {}
            for key, e_now in energies.items():
                e_prev = self._last_energy.get(key)
                if e_prev is None:
                    continue
                delta_uj = e_now - e_prev
                # Wrap-around: energy_uj is a 64-bit counter on modern kernels,
                # but older ones wrap at max_energy_range_uj.
                if delta_uj < 0:
                    domain = next((d for d in self._domains if d.key == key), None)
                    if domain and domain.max_uj > 0:
                        delta_uj += domain.max_uj
                    else:
                        # No max known — skip this sample, will re-sync next tick
                        continue
                # µJ / s = µW; / 1e6 -> W
                power_w[key] = delta_uj / dt / 1e6

            if power_w:
                partial["cpu"] = {"power_w": power_w}

            self._last_energy = energies
            self._last_ts = now
            self._cost.record_us(sw.elapsed_us)
            return partial


def make() -> RaplEnergyCollector | None:
    from coolstep.compat import caps_if_set
    _c = caps_if_set()
    # Skip only when caps are loaded AND neither signal is present.
    # hwmon_rapl: Intel-RAPL via powercap path. rapl_powercap: presence of
    # /sys/class/powercap/intel-rapl tree (same powercap interface is reused
    # on modern AMD kernels). Either one alone is enough; discover() handles
    # the rest.
    if _c is not None and not _c.hwmon_rapl and not _c.rapl_powercap:
        return None
    collector = RaplEnergyCollector()
    return collector if collector.discover() else None
