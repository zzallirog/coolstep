"""Linux native collector — hwmon + cpufreq + platform_state.

Reads:
- /sys/class/hwmon/* — temps, fans, voltages, power (whatever hwmon exposes)
- /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq → freq_mhz per-core
- /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor → platform_state
- /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference → platform_state
- /sys/firmware/acpi/platform_profile → platform_state
- /proc/stat (delta-based) → load_pct per-core

Hwmon `name` files are used to classify devices into:
  - cpu temps (k10temp, coretemp, zenpower, k8temp)
  - gpu temps (amdgpu, nouveau)  — coarse; pynvml/amdgpu collectors override
  - storage (nvme)
  - memory (spd5118)
  - fans (asus, dell-smm, etc.)

P2.5 (perf) — path cache. Hardware topology is stable across ticks
(hwmon devices, sensor labels, per-core cpufreq paths don't appear or
vanish at runtime unless the user hot-plugs something), so we discover
each path once and remember it on the instance. Without this every
tick re-glob'd ~5-6 directories and re-resolved ~35 paths via
`Path.glob()` — ~18 ms total. With the cache: one open() per sensor,
no globs after the first sample. TTL rebuild every PATH_CACHE_TTL_S
seconds keeps the hot-plug / driver-reload case alive.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from coolstep.adapters.collectors._base import CostTracker, stopwatch
from coolstep.core.schema import Cost, FanMetrics, SignalDescriptor

log = logging.getLogger(__name__)

# HWMON driver name sets — populated from the layered manifest at import time.
# Community can add new chips via /etc/coolstep/community.json; user can override
# via ~/.config/coolstep/custom.json.  See coolstep/compat/core.json + manifest.py.
def _hwmon_set(key: str) -> frozenset[str]:
    from coolstep.compat.manifest import load_manifest
    return frozenset(load_manifest().get("hwmon", {}).get(key, []))


CPU_HWMON_NAMES = _hwmon_set("cpu_drivers")
STORAGE_HWMON_NAMES = _hwmon_set("storage_drivers")
MEMORY_HWMON_NAMES = _hwmon_set("memory_drivers")
FAN_HWMON_NAMES = _hwmon_set("fan_drivers")
SKIP_HWMON_NAMES = _hwmon_set("skip_drivers")

# Sanity check at import time: empty CPU/FAN sets mean the manifest is
# missing critical keys (corrupt core.json or aggressive L2 _disable
# override).  Warn once so a silent collector doesn't go undetected.
if not CPU_HWMON_NAMES:
    log.warning("linux_sysfs: hwmon.cpu_drivers empty — CPU temp detection disabled")
if not FAN_HWMON_NAMES:
    log.warning("linux_sysfs: hwmon.fan_drivers empty — fan RPM detection disabled")

# Hardware topology is stable; rebuild the path cache rarely. 60 s is
# long enough to ignore tick-rate cost yet short enough that a USB-C
# adapter (un)plug becomes visible within ~minutes.
PATH_CACHE_TTL_S = 60.0

# Stage-3 selective-refresh intervals (ticks). The fingerprint extractor
# reads cpu_freq into the 26-dim embedding, but its 30 s rolling window
# absorbs 1-3 s staleness without changing predictor behaviour.
FREQ_REFRESH_TICKS = 3
VOLTAGE_REFRESH_TICKS = 5
PLATFORM_STATE_REFRESH_TICKS = 30


def _read_text(path: Path) -> str | None:
    """Plain `read_text` for one-shot path-resolution paths (labels,
    discover-time reads). Not for hot per-tick I/O — use `_FdCache`
    on the collector for those."""
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


class _FdCache:
    """Stage-2 perf win — open() each sysfs path ONCE, re-use the file
    descriptor via `os.pread(fd, n, 0)` on subsequent ticks.

    /sys files are seekable; reading at offset 0 always returns the
    current value of the underlying kernel attribute. This saves the
    open() + close() syscall pair per read — about 400-500µs on the
    target laptop. With ~30 reads per tick that's ~12-15 ms reclaimed.

    Lifecycle:
      * `read_text(path)` opens lazily on first call, then re-uses.
      * `reset()` closes everything; called when the path cache rebuilds
        so we don't hold fds for vanished hwmon devices.
      * Any EBADF (e.g. driver reload) bubbles as None and the fd is
        evicted so the next call re-opens.
      * No reference-count: the cache is owned by one collector
        instance which is single-threaded per-tick.
    """

    # /sys attribute reads are small (typically <64 bytes for a temp,
    # ~256 bytes for /proc/stat lines, ~4 KB worst case for /proc/stat
    # whole-file reads). 8 KB buffer covers all of them with one syscall.
    BUF_SIZE = 8192

    def __init__(self) -> None:
        self._fds: dict[Path, int] = {}

    def read_text(self, path: Path) -> str | None:
        fd = self._fds.get(path)
        if fd is None:
            try:
                fd = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC)
            except OSError:
                return None
            self._fds[path] = fd
        try:
            raw = os.pread(fd, self.BUF_SIZE, 0)
        except OSError:
            # Stale fd (driver reload / removed device). Evict + bubble
            # None so the caller can choose to re-resolve the path.
            self._evict(path)
            return None
        try:
            return raw.decode("utf-8", errors="replace").strip()
        except (UnicodeDecodeError, AttributeError):
            return None

    def read_int(self, path: Path) -> int | None:
        text = self.read_text(path)
        if text is None:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def _evict(self, path: Path) -> None:
        fd = self._fds.pop(path, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def reset(self) -> None:
        for fd in self._fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()

    def __del__(self) -> None:
        # Best-effort close on collector dealloc. systemd shutdown will
        # close all fds anyway, but explicit cleanup helps the test
        # suite avoid ResourceWarning under -W error.
        self.reset()


class LinuxSysfsCollector:
    name = "linux_sysfs"

    def __init__(
        self,
        hwmon_root: Path = Path("/sys/class/hwmon"),
        cpu_root: Path = Path("/sys/devices/system/cpu"),
        proc_stat_path: Path = Path("/proc/stat"),
        platform_profile_path: Path = Path("/sys/firmware/acpi/platform_profile"),
    ) -> None:
        self.hwmon_root = hwmon_root
        self.cpu_root = cpu_root
        self.proc_stat_path = proc_stat_path
        self.platform_profile_path = platform_profile_path
        self._cost = CostTracker()
        self._prev_proc_stat: dict[str, tuple[int, int]] = {}  # cpu_id -> (idle, total)
        self._prev_iowait: dict[str, int] = {}  # cpu_id -> iowait counter
        # P2.5 Stage-2 — fd cache for hot per-tick reads. Owned by the
        # collector, reset whenever the path cache rebuilds.
        self._fds = _FdCache()
        # P2.5 Stage-3 — selective refresh. Some signals don't move fast
        # enough that we should pay their syscall cost every tick:
        #   * cpu freq per-core: governor adjusts at kHz scale; the 30s
        #     predictor window doesn't care about a 1-3 s staleness.
        #   * voltages: similar — slow to move on a thermally-stable chip.
        #   * platform_state: user config (governor / epp / boost), changes
        #     on minute scale at most.
        # Counters advance on each successful sample. The cached value is
        # returned between refresh ticks.
        self._tick_counter: int = 0
        self._freq_cache: list[float] = []
        self._voltage_cache: dict[str, float] = {}
        self._platform_state_cache: dict[str, str] = {}

        # ── P2.5 path cache ──────────────────────────────────────────
        # Mirrors the discovery walk that `_sample_inner` used to do
        # inline every tick. Each entry of `_hwmon_devices` carries the
        # device's classification + pre-resolved sensor paths so the hot
        # tick path just reads from known offsets — no Path.glob().
        # Empty until the first sample (lazy build keeps `discover()`
        # cheap).
        self._paths_at: float = 0.0
        self._hwmon_devices: list[dict] = []
        self._cpu_freq_paths: list[Path] = []
        # Static platform-state paths — built once in __init__; never
        # re-globbed since these are well-known kernel sysfs entries.
        self._platform_state_paths: dict[str, Path] = {
            "governor": cpu_root / "cpu0" / "cpufreq" / "scaling_governor",
            "epp": cpu_root / "cpu0" / "cpufreq" / "energy_performance_preference",
            "platform_profile": platform_profile_path,
            "boost": cpu_root / "cpufreq" / "boost",
        }

    def discover(self) -> bool:
        return self.hwmon_root.is_dir() and self.cpu_root.is_dir()

    def cost(self) -> Cost:
        return Cost(sample_us=self._cost.avg_us(), rss_kb=0)

    def signals(self) -> list[SignalDescriptor]:
        return [
            SignalDescriptor(
                name="cpu.freq_mhz",
                unit="MHz", dtype="list[float]", cardinality="per-core",
                source="/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq",
                description="Per-core current scaling frequency",
            ),
            SignalDescriptor(
                name="cpu.load_pct",
                unit="%", dtype="list[float]", cardinality="per-core",
                source="/proc/stat (delta-based, iowait counted as idle)",
                description="Per-core load percentage; thermally-correct "
                            "(iowait not counted as execution)",
            ),
            SignalDescriptor(
                name="cpu.iowait_pct",
                unit="%", dtype="list[float]", cardinality="per-core",
                source="/proc/stat (delta-based)",
                description="Per-core iowait %. Disk-bound vs true-idle "
                            "discriminator для P1 workload classifier'а",
            ),
            SignalDescriptor(
                name="cpu.temps_c",
                unit="°C", dtype="dict[str, float]", cardinality="dict",
                source="/sys/class/hwmon/hwmonN/temp*_input (k10temp/coretemp/zenpower)",
                description="Tctl, Tdie, package temps via vendor labels",
            ),
            SignalDescriptor(
                name="cpu.power_w",
                unit="W", dtype="dict[str, float]", cardinality="dict",
                source="/sys/class/hwmon/hwmonN/power*_input (RAPL where exposed)",
                description="Package and per-core power; AMD k10temp not always exposes",
            ),
            SignalDescriptor(
                name="cpu.voltage_v",
                unit="V", dtype="dict[str, float]", cardinality="dict",
                source="/sys/class/hwmon/hwmonN/in*_input",
            ),
            SignalDescriptor(
                name="storage_temps_c",
                unit="°C", dtype="dict[str, float]", cardinality="per-device",
                source="/sys/class/hwmon/hwmonN (nvme)",
                description="NVMe SSD temperatures (Composite, Sensor 1/2)",
            ),
            SignalDescriptor(
                name="memory_temps_c",
                unit="°C", dtype="dict[str, float]", cardinality="per-device",
                source="/sys/class/hwmon/hwmonN (spd5118)",
                description="DRAM module temperatures (DDR5 SPD5118)",
            ),
            SignalDescriptor(
                name="fans",
                unit="RPM", dtype="list[FanMetrics]", cardinality="per-device",
                source="/sys/class/hwmon/hwmonN (asus, asus_custom_fan_curve, ...)",
                description="Fan tachometer + PWM where exposed",
            ),
            SignalDescriptor(
                name="platform_state.governor",
                unit="enum", dtype="str", cardinality="scalar",
                source="/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
            ),
            SignalDescriptor(
                name="platform_state.epp",
                unit="enum", dtype="str", cardinality="scalar",
                source="/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference",
                description="amd-pstate-epp / intel_pstate EPP preference",
            ),
            SignalDescriptor(
                name="platform_state.platform_profile",
                unit="enum", dtype="str", cardinality="scalar",
                source="/sys/firmware/acpi/platform_profile",
                description="ASUS Quiet/Balanced/Performance",
            ),
            SignalDescriptor(
                name="platform_state.boost",
                unit="bool", dtype="str", cardinality="scalar",
                source="/sys/devices/system/cpu/cpufreq/boost",
            ),
        ]

    # ── P2.5 path cache build ────────────────────────────────────────

    def _refresh_paths(self) -> None:
        """One-shot enumeration of every hwmon device + cpufreq path.

        Mirrors the discovery walk that used to live in `_sample_inner`.
        Called from `_sample_inner` only when the cache is empty OR the
        TTL has expired. Failures are silent — a missing label file
        just leaves the per-sensor fallback name in place; we never
        raise from here. """
        # Stage-2: close every fd from the previous incarnation. Stale
        # fds for vanished hwmon devices would just collect garbage and
        # could fail unpredictably on a driver hot-reload.
        self._fds.reset()
        devices: list[dict] = []
        try:
            hwmon_dirs = sorted(self.hwmon_root.glob("hwmon*"))
        except OSError:
            hwmon_dirs = []
        for hwmon_dir in hwmon_dirs:
            name = _read_text(hwmon_dir / "name")
            if not name or name in SKIP_HWMON_NAMES:
                continue
            if name in CPU_HWMON_NAMES:
                kind = "cpu"
            elif name in STORAGE_HWMON_NAMES:
                kind = "storage"
            elif name in MEMORY_HWMON_NAMES:
                kind = "memory"
            elif name in FAN_HWMON_NAMES:
                kind = "fan"
            else:
                # gpu hwmon (amdgpu, nouveau) intentionally skipped —
                # see CLAUDE.md FAQ about double-record-in-frame.gpus.
                continue
            entry: dict = {
                "name": name,
                "dir": hwmon_dir,
                "kind": kind,
                "temps": [],     # list of (label, input_path)
                "voltages": [],
                "powers": [],
                "fans": [],      # list of (label, input_path, pwm_path)
            }
            if kind in {"cpu", "storage", "memory"}:
                # Label prefix mirrors the legacy free-function code: cpu
                # uses bare device-name; storage/memory namespace by
                # hwmonN id so multiple NVMes / DIMMs don't collide.
                prefix = name if kind == "cpu" else f"{name}{hwmon_dir.name}"
                try:
                    temp_inputs = sorted(hwmon_dir.glob("temp*_input"))
                except OSError:
                    temp_inputs = []
                for input_path in temp_inputs:
                    idx = input_path.name.removeprefix("temp").removesuffix("_input")
                    label_path = hwmon_dir / f"temp{idx}_label"
                    label = _read_text(label_path) or f"{prefix}_temp{idx}"
                    entry["temps"].append((label.lower(), input_path))
                if kind == "cpu":
                    try:
                        v_inputs = sorted(hwmon_dir.glob("in*_input"))
                    except OSError:
                        v_inputs = []
                    for input_path in v_inputs:
                        idx = input_path.name.removeprefix("in").removesuffix("_input")
                        label_path = hwmon_dir / f"in{idx}_label"
                        label = _read_text(label_path) or f"{prefix}_in{idx}"
                        entry["voltages"].append((label.lower(), input_path))
                    try:
                        p_inputs = sorted(hwmon_dir.glob("power*_input"))
                    except OSError:
                        p_inputs = []
                    for input_path in p_inputs:
                        idx = input_path.name.removeprefix("power").removesuffix("_input")
                        label_path = hwmon_dir / f"power{idx}_label"
                        label = _read_text(label_path) or f"{prefix}_power{idx}"
                        entry["powers"].append((label.lower(), input_path))
            elif kind == "fan":
                try:
                    f_inputs = sorted(hwmon_dir.glob("fan*_input"))
                except OSError:
                    f_inputs = []
                for input_path in f_inputs:
                    idx = input_path.name.removeprefix("fan").removesuffix("_input")
                    label_path = hwmon_dir / f"fan{idx}_label"
                    label = _read_text(label_path) or f"{name}_fan{idx}"
                    pwm_path = hwmon_dir / f"pwm{idx}"
                    entry["fans"].append((label, input_path, pwm_path))
            devices.append(entry)
        self._hwmon_devices = devices

        # Per-core cpufreq paths — Ryzen 7940HS has 16 logical CPUs, so
        # this is 16 readdir entries; was re-globbed every tick.
        try:
            self._cpu_freq_paths = [
                cpu_dir / "cpufreq" / "scaling_cur_freq"
                for cpu_dir in sorted(self.cpu_root.glob("cpu[0-9]*"))
            ]
        except OSError:
            self._cpu_freq_paths = []

    # ── sample ────────────────────────────────────────────────────────

    def sample(self) -> dict[str, object]:
        with stopwatch() as sw:
            partial = self._sample_inner()
        self._cost.record_us(sw.elapsed_us)
        return partial

    def _sample_inner(self) -> dict[str, object]:
        now = time.monotonic()
        if (
            not self._hwmon_devices
            or (now - self._paths_at) > PATH_CACHE_TTL_S
        ):
            self._refresh_paths()
            self._paths_at = now

        self._tick_counter += 1
        cpu_temps: dict[str, float] = {}
        cpu_voltages: dict[str, float] = self._voltage_cache
        cpu_powers: dict[str, float] = {}
        storage_temps: dict[str, float] = {}
        memory_temps: dict[str, float] = {}
        fans: list[FanMetrics] = []

        # Selective-refresh schedule. Temps + powers + fans are read
        # every tick (they swing fast enough to matter for prediction);
        # voltages on the slower path. Cache `self._voltage_cache` is
        # already wired as the working sink above; we just decide
        # whether to actually re-read on this tick.
        do_voltages = (self._tick_counter % VOLTAGE_REFRESH_TICKS) == 1
        if do_voltages:
            cpu_voltages = {}
        for device in self._hwmon_devices:
            kind = device["kind"]
            if kind == "cpu":
                self._read_into(device["temps"], cpu_temps, scale=1000.0)
                if do_voltages:
                    self._read_into(device["voltages"], cpu_voltages, scale=1000.0)
                self._read_into(device["powers"], cpu_powers, scale=1_000_000.0)
            elif kind == "storage":
                self._read_into(device["temps"], storage_temps, scale=1000.0)
            elif kind == "memory":
                self._read_into(device["temps"], memory_temps, scale=1000.0)
            elif kind == "fan":
                for label, input_path, pwm_path in device["fans"]:
                    rpm = self._fds.read_int(input_path)
                    pwm = self._fds.read_int(pwm_path)
                    fans.append(FanMetrics(name=label, rpm=rpm, pwm=pwm))

        # Stage-3 freq + platform-state refresh decisions. /proc/stat is
        # always read because load% is the predictor's primary signal.
        do_freq = (self._tick_counter % FREQ_REFRESH_TICKS) == 1
        do_platform = (self._tick_counter % PLATFORM_STATE_REFRESH_TICKS) == 1
        if do_freq:
            self._freq_cache = self._read_freqs()
        if do_platform:
            self._platform_state_cache = self._read_platform_state()
        if do_voltages:
            self._voltage_cache = cpu_voltages

        loads, iowaits = self._read_load()
        partial: dict[str, object] = {
            "cpu": {
                "freq_mhz": list(self._freq_cache),
                "load_pct": loads,
                "iowait_pct": iowaits,
                "temps_c": _normalize_cpu_temps(cpu_temps),
                "voltage_v": dict(self._voltage_cache),
                "power_w": cpu_powers,
            },
            "storage_temps_c": storage_temps,
            "memory_temps_c": memory_temps,
            "fans": fans,
            "platform_state": dict(self._platform_state_cache),
        }
        return partial

    def _read_into(
        self,
        entries: list[tuple[str, Path]],
        sink: dict[str, float],
        *,
        scale: float,
    ) -> None:
        """Read each (label, path) pair into `sink[label] = raw / scale`.

        Uses the instance fd cache — `entries` paths are pre-resolved by
        `_refresh_paths()`, and `_FdCache` keeps each fd open across
        ticks. Stage-2 of the perf work."""
        for label, input_path in entries:
            raw = self._fds.read_int(input_path)
            if raw is None:
                continue
            sink[label] = raw / scale

    def _read_freqs(self) -> list[float]:
        freqs: list[float] = []
        for path in self._cpu_freq_paths:
            khz = self._fds.read_int(path)
            if khz is not None:
                freqs.append(khz / 1000.0)
        return freqs

    def _read_load(self) -> tuple[list[float], list[float]]:
        """Returns (load_pct, iowait_pct) per per-core line из /proc/stat.

        load_pct **включает iowait в idle** (т.е. iowait не считается за
        load). Обоснование: ядро в iowait не исполняет инструкции — не
        генерирует heat. Для thermal-prediction load metric это правильно.

        Отдельный iowait_pct сигнал для workload classifier'а (P1+):
        позволит ему отличать disk-bound фазу (CPU свободно ждёт I/O) от
        true-idle (системе нечего делать). Текущий P0 fingerprint не
        использует — emit'ится для будущих ML признаков.
        """
        loads: list[float] = []
        iowaits: list[float] = []
        # /proc/stat via fd-cache too — same open-cost saving as sysfs.
        text = self._fds.read_text(self.proc_stat_path)
        if text is None:
            return loads, iowaits
        for line in text.splitlines():
            if not line.startswith("cpu") or line.startswith("cpu "):
                continue
            parts = line.split()
            cpu_id = parts[0]
            try:
                values = [int(x) for x in parts[1:]]
            except ValueError:
                continue
            if len(values) < 5:
                continue
            iowait_raw = values[4] if len(values) > 4 else 0
            idle = values[3] + iowait_raw
            total = sum(values)
            prev = self._prev_proc_stat.get(cpu_id)
            prev_iowait = self._prev_iowait.get(cpu_id, 0)
            self._prev_proc_stat[cpu_id] = (idle, total)
            self._prev_iowait[cpu_id] = iowait_raw
            if prev is None:
                loads.append(0.0)
                iowaits.append(0.0)
                continue
            d_idle = idle - prev[0]
            d_total = total - prev[1]
            d_iowait = iowait_raw - prev_iowait
            if d_total <= 0:
                loads.append(0.0)
                iowaits.append(0.0)
            else:
                loads.append(max(0.0, min(100.0, 100.0 * (1.0 - d_idle / d_total))))
                iowaits.append(max(0.0, min(100.0, 100.0 * d_iowait / d_total)))
        return loads, iowaits

    def _read_platform_state(self) -> dict[str, str]:
        state: dict[str, str] = {}
        for key, path in self._platform_state_paths.items():
            val = self._fds.read_text(path)
            if val:
                state[key] = val
        return state


def _normalize_cpu_temps(raw: dict[str, float]) -> dict[str, float]:
    """Map vendor labels to canonical names used downstream.

    Keep originals too (so dashboard can show raw labels), but ensure 'tctl' /
    'tdie' / 'package' shortcuts exist when present in any form.
    """
    out = dict(raw)
    aliases = {
        "tctl": ("tctl", "tctl_temp", "tctl °c"),
        "tdie": ("tdie", "tdie_temp"),
        "package": ("package id 0", "package", "package_temp"),
    }
    for canonical, names in aliases.items():
        if canonical in out:
            continue
        for n in names:
            if n in out:
                out[canonical] = out[n]
                break
    return out


def make() -> LinuxSysfsCollector | None:
    collector = LinuxSysfsCollector()
    return collector if collector.discover() else None
