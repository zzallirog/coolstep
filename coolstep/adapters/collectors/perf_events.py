"""perf_event_open — PMU counters via long-running `perf stat -I 1000`.

Real implementation: spawns one `perf stat -e <events> -I 1000 -x , -a`
subprocess at discover() and reads its stdout in a background daemon
thread.  sample() returns a snapshot of the latest values — O(1), no fork.

Permission:
    /proc/sys/kernel/perf_event_paranoid:
       -1   = anything
        0   = CPU events + tracepoints OK for non-root
        1   = per-process events OK (default on Debian/Ubuntu)
        2   = per-process + group events for current user (default on Arch)
        3   = nothing for non-root (RH-strict / hardened)
    Server kernels often pin paranoid=3 — discover() returns False and
    install_plan emits a pointer with the unlock command.

Signals (filled into TelemetryFrame.platform_state and cpu.*):
    cpu.pmu.cycles            — float events/s
    cpu.pmu.instructions      — float events/s
    cpu.pmu.cache_misses      — float events/s
    cpu.pmu.cache_references  — float events/s
    cpu.pmu.cpi               — derived = cycles / instructions
    cpu.pmu.cache_miss_pct    — derived = cache_misses / cache_references

Output is folded into TelemetryFrame.platform_state as string values
(cheapest schema integration); a future schema bump can promote them
to first-class cpu fields.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from coolstep.adapters.collectors._base import CostTracker, stopwatch
from coolstep.core.schema import Cost, SignalDescriptor

log = logging.getLogger(__name__)

_PARANOID_FILE = Path("/proc/sys/kernel/perf_event_paranoid")

# Events that work on most x86_64 with paranoid<=2.  AMD Zen / Intel CPUs both
# expose the symbolic names below via the generic perf interface.
_DEFAULT_EVENTS = (
    "cycles",
    "instructions",
    "cache-misses",
    "cache-references",
    "task-clock",
)

_PERF_INTERVAL_MS = 1000  # -I 1000 → one report per second

# perf stat CSV layout (with -x ,) per kernel docs:
#   <timestamp>,<value>,<unit>,<event>,<run_time>,<run_pct>
# When `-x ,` and -a present, the line may also include CPU prefix.
_EXPECTED_FIELDS = 6


def _read_paranoid() -> int | None:
    try:
        return int(_PARANOID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


class PerfEventsCollector:
    name = "perf_events"

    def __init__(
        self,
        events: tuple[str, ...] = _DEFAULT_EVENTS,
        interval_ms: int = _PERF_INTERVAL_MS,
    ) -> None:
        self._cost = CostTracker()
        self._events = events
        self._interval_ms = interval_ms
        self._paranoid: int | None = None
        self._proc: subprocess.Popen | None = None
        self._pgid: int | None = None  # cached at spawn() — see close()
        self._reader_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest: dict[str, float] = {}
        self._stopping = threading.Event()

    # ------------------------------------------------------------------
    def discover(self) -> bool:
        self._paranoid = _read_paranoid()
        if self._paranoid is None or self._paranoid > 2:
            return False
        if not shutil.which("perf"):
            return False

        if not self._spawn():
            return False
        # Confirm perf didn't die immediately (e.g. kernel rejects -a
        # without CAP_PERFMON despite paranoid<=2).  Modern Linux kernels
        # require CAP_PERFMON even at paranoid=2 for system-wide profiling.
        time.sleep(0.3)
        if self._proc and self._proc.poll() is not None:
            log.info(
                "perf_events: subprocess exited rc=%s within 300ms — likely "
                "kernel requires CAP_PERFMON for system-wide -a profiling",
                self._proc.returncode,
            )
            self._proc = None
            return False
        return True

    def _spawn(self) -> bool:
        cmd = [
            "perf", "stat",
            "-e", ",".join(self._events),
            "-I", str(self._interval_ms),
            "-x", ",",
            "-a",  # all CPUs
        ]
        try:
            # stderr → stdout because perf prints stats to stderr by default
            self._proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                # Detach from our process group so a SIGINT on coolstep
                # doesn't half-kill perf mid-write
                start_new_session=True,
            )
        except OSError as exc:
            log.warning("perf_events: spawn failed: %r", exc)
            return False

        # Cache the process-group id immediately — proc.pid can become stale
        # (or refer to a recycled pid) by the time close() runs.
        try:
            self._pgid = os.getpgid(self._proc.pid)
        except (ProcessLookupError, PermissionError, OSError):
            self._pgid = None

        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="perf-events-reader", daemon=True
        )
        self._reader_thread.start()
        return True

    # ------------------------------------------------------------------
    def _reader_loop(self) -> None:
        assert self._proc is not None
        stdout = self._proc.stdout
        assert stdout is not None
        # Per-interval accumulator: perf prints N events × M lines per interval;
        # they share a timestamp.  We flush into self._latest when timestamp changes.
        current_ts: str = ""
        acc: dict[str, float] = {}

        try:
            for line in stdout:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # perf -x , output: <ts>,<value>,<unit>,<event>,<run_ns>,<run_pct>
                # When event is "<not counted>" the value field is "<not counted>"
                fields = line.split(",")
                if len(fields) < _EXPECTED_FIELDS:
                    continue
                ts_str = fields[0].strip()
                value_str = fields[1].strip()
                event = fields[3].strip()

                if value_str in {"<not counted>", "<not supported>"}:
                    continue
                try:
                    value = float(value_str)
                except ValueError:
                    continue

                if current_ts and ts_str != current_ts and acc:
                    # New interval started — promote previous batch
                    self._publish(acc)
                    acc = {}
                current_ts = ts_str
                acc[event] = value
        except Exception as exc:  # noqa: BLE001
            log.debug("perf_events reader exiting: %r", exc)
        finally:
            if acc:
                self._publish(acc)

    def _publish(self, raw_events: dict[str, float]) -> None:
        """Convert raw event counts (per interval) → rates + derived fields."""
        period_s = self._interval_ms / 1000.0
        with self._lock:
            # rates (events/s)
            for ev, count in raw_events.items():
                self._latest[f"pmu.{ev.replace('-', '_')}"] = count / period_s
            # derived
            cycles = raw_events.get("cycles")
            insts = raw_events.get("instructions")
            misses = raw_events.get("cache-misses")
            refs = raw_events.get("cache-references")
            tc = raw_events.get("task-clock")  # ms of CPU-time
            if cycles and insts and insts > 0:
                self._latest["pmu.cpi"] = cycles / insts
            if misses is not None and refs and refs > 0:
                self._latest["pmu.cache_miss_pct"] = 100.0 * misses / refs
            if tc is not None:
                # task-clock is reported in *milliseconds of CPU time per interval*.
                # Convert to "CPUs busy" fraction: tc / (interval_ms × ncpu) × 100
                self._latest["pmu.task_clock_ms"] = tc

    # ------------------------------------------------------------------
    def cost(self) -> Cost:
        return Cost(sample_us=self._cost.avg_us(), rss_kb=0)

    def signals(self) -> list[SignalDescriptor]:
        return [
            SignalDescriptor(
                name="cpu.pmu.cycles", unit="events/s", dtype="float",
                source="perf stat -e cycles -I 1000",
                cardinality="scalar",
                description="CPU cycles per second (frequency × util × cores)",
                requires=("perf binary", "perf_event_paranoid<=2"),
            ),
            SignalDescriptor(
                name="cpu.pmu.instructions", unit="events/s", dtype="float",
                source="perf stat -e instructions",
                cardinality="scalar",
                description="Retired instructions/s",
                requires=("perf binary",),
            ),
            SignalDescriptor(
                name="cpu.pmu.cpi", unit="ratio", dtype="float",
                source="cycles / instructions (derived)",
                cardinality="scalar",
                description="Cycles-per-instruction — high CPI = stalls",
                requires=("perf binary",),
            ),
            SignalDescriptor(
                name="cpu.pmu.cache_miss_pct", unit="%", dtype="float",
                source="cache-misses / cache-references (derived)",
                cardinality="scalar",
                description="LLC miss rate — early heat-bound predictor",
                requires=("perf binary",),
            ),
        ]

    # ------------------------------------------------------------------
    def sample(self) -> dict[str, object]:
        with stopwatch() as sw:
            with self._lock:
                snapshot = dict(self._latest)
            partial: dict[str, object] = {}
            if snapshot:
                # Promote into platform_state as strings (cheapest schema fit)
                partial["platform_state"] = {
                    f"perf.{k}": f"{v:.2f}" for k, v in snapshot.items()
                }
            self._cost.record_us(sw.elapsed_us)
            return partial

    # ------------------------------------------------------------------
    def close(self) -> None:
        """Terminate the long-running perf subprocess.

        Called explicitly by the daemon shutdown path.  Best-effort:
        we send SIGTERM to the cached process group, wait up to 2s,
        then SIGKILL.  The reader thread is joined briefly so it can't
        publish stale data after close() returns.
        """
        self._stopping.set()
        proc = self._proc
        if proc is None:
            return

        def _signal_group(sig: int) -> None:
            """Send a signal to the cached pgid, falling back to proc."""
            if self._pgid is not None:
                try:
                    os.killpg(self._pgid, sig)
                    return
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            try:
                if sig == signal.SIGTERM:
                    proc.terminate()
                else:
                    proc.kill()
            except Exception:  # noqa: BLE001
                pass

        try:
            _signal_group(signal.SIGTERM)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                _signal_group(signal.SIGKILL)
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    log.warning("perf_events: subprocess pid=%s did not exit after SIGKILL", proc.pid)
        finally:
            # Best-effort thread cleanup. Daemonized so it won't block exit,
            # but join briefly so it can't update self._latest after close().
            thread = self._reader_thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)
                if thread.is_alive():
                    log.debug("perf_events: reader thread did not join within 1s")
            self._proc = None
            self._pgid = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


def make() -> PerfEventsCollector | None:
    from coolstep.compat import caps_if_set
    _c = caps_if_set()
    if _c is not None and (
        not _c.perf_binary_available or _c.perf_event_paranoid > 2
    ):
        return None
    collector = PerfEventsCollector()
    return collector if collector.discover() else None
