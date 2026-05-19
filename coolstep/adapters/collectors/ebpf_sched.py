"""eBPF scheduler events — context-switch / fork rate via bpftrace JSON.

Real implementation: spawns `bpftrace -f json` as a long-running subprocess,
reads JSON-line events in a background thread, exposes per-second counters
as a snapshot for sample().

Why bpftrace, not BCC: bpftrace ships as a single binary with embedded
clang+llvm; BCC requires Python bindings + matching kernel headers.
The product positioning is "low-overhead pre-load detection" — easier
deployment beats marginally lower runtime cost.

Permission: CAP_PERFMON (kernel ≥ 5.8) or CAP_SYS_ADMIN (older).
Recommended wiring in systemd unit:
    AmbientCapabilities=CAP_PERFMON
    NoNewPrivileges=false  # CAP_PERFMON requires this

Signals:
    cpu.ctx_switch_rate   — list[float] per-CPU (context switches/s)
    cpu.fork_rate         — float (sched_process_fork events/s)
    cpu.exec_rate         — float (sched_process_exec/s)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path

from coolstep.adapters.collectors._base import CostTracker, stopwatch
from coolstep.core.schema import Cost, SignalDescriptor

log = logging.getLogger(__name__)

# bpftrace script: one second window aggregates, three maps published per tick.
_BPFTRACE_SCRIPT = r"""
tracepoint:sched:sched_switch { @switches[cpu] = count(); }
tracepoint:sched:sched_process_fork { @forks = count(); }
tracepoint:sched:sched_process_exec { @execs = count(); }
interval:s:1 {
    print(@switches);
    print(@forks);
    print(@execs);
    clear(@switches);
    clear(@forks);
    clear(@execs);
}
"""

# Indicators that the kernel supports the tracepoints we need
_SCHED_SWITCH_TRACE = Path("/sys/kernel/tracing/events/sched/sched_switch")
_SCHED_SWITCH_TRACE_DEBUG = Path("/sys/kernel/debug/tracing/events/sched/sched_switch")
_BPF_FS = Path("/sys/fs/bpf")


def _can_run_bpf() -> bool:
    if not _BPF_FS.is_dir():
        return False
    return _SCHED_SWITCH_TRACE.is_dir() or _SCHED_SWITCH_TRACE_DEBUG.is_dir()


class EbpfSchedCollector:
    name = "ebpf_sched"

    def __init__(self) -> None:
        self._cost = CostTracker()
        self._proc: subprocess.Popen | None = None
        self._pgid: int | None = None  # cached at spawn() — see close()
        self._reader_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest: dict[str, object] = {}
        self._stopping = threading.Event()

    # ------------------------------------------------------------------
    def discover(self) -> bool:
        if not _can_run_bpf():
            return False
        if not shutil.which("bpftrace"):
            return False
        return self._spawn()

    def _spawn(self) -> bool:
        cmd = ["bpftrace", "-f", "json", "-e", _BPFTRACE_SCRIPT]
        try:
            self._proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            log.warning("ebpf_sched: spawn failed: %r", exc)
            return False

        # Cache pgid at spawn — proc.pid can be stale by close() time.
        try:
            self._pgid = os.getpgid(self._proc.pid)
        except (ProcessLookupError, PermissionError, OSError):
            self._pgid = None

        # Quick liveness check: if bpftrace fails on permission, it usually
        # exits within ~100 ms.  We can't block here without races, so we
        # let _reader_loop notice and bail.
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="ebpf-sched-reader", daemon=True,
        )
        self._reader_thread.start()
        return True

    # ------------------------------------------------------------------
    def _reader_loop(self) -> None:
        assert self._proc is not None
        stdout = self._proc.stdout
        assert stdout is not None
        try:
            for line in stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "map":
                    continue
                self._consume_map(obj.get("data", {}))
        except Exception as exc:  # noqa: BLE001
            log.debug("ebpf_sched reader exiting: %r", exc)

    def _consume_map(self, data: dict) -> None:
        """bpftrace map payload — `{"@switches": {"0": 1234, "1": 2345}}` or
        scalar `{"@forks": 567}`."""
        with self._lock:
            for key, val in data.items():
                # Trim leading "@" — bpftrace map sentinel
                clean = key.lstrip("@")
                if isinstance(val, dict):
                    # per-CPU map → list[float], indexed by CPU id
                    if not val:
                        continue
                    max_cpu = max(int(k) for k in val)
                    out = [0.0] * (max_cpu + 1)
                    for k, v in val.items():
                        try:
                            out[int(k)] = float(v)
                        except (ValueError, IndexError):
                            continue
                    self._latest[clean] = out
                elif isinstance(val, (int, float)):
                    self._latest[clean] = float(val)
                # other types ignored

    # ------------------------------------------------------------------
    def cost(self) -> Cost:
        return Cost(sample_us=self._cost.avg_us(), rss_kb=0)

    def signals(self) -> list[SignalDescriptor]:
        return [
            SignalDescriptor(
                name="cpu.ctx_switch_rate", unit="ctx/s", dtype="list[float]",
                source="bpftrace tracepoint:sched:sched_switch",
                cardinality="per-core",
                description="Per-CPU context-switch rate — pre-load indicator",
                requires=("bpftrace", "CAP_PERFMON or CAP_SYS_ADMIN"),
            ),
            SignalDescriptor(
                name="cpu.fork_rate", unit="fork/s", dtype="float",
                source="tracepoint:sched:sched_process_fork",
                cardinality="scalar",
                description="Process creation rate — proxy for build/render bursts",
                requires=("bpftrace",),
            ),
            SignalDescriptor(
                name="cpu.exec_rate", unit="exec/s", dtype="float",
                source="tracepoint:sched:sched_process_exec",
                cardinality="scalar",
                description="execve rate — heavy when launching games / compiles",
                requires=("bpftrace",),
            ),
        ]

    # ------------------------------------------------------------------
    def sample(self) -> dict[str, object]:
        with stopwatch() as sw:
            with self._lock:
                snap = dict(self._latest)
            partial: dict[str, object] = {}
            if snap:
                # Surface in platform_state as strings for now
                ps: dict[str, str] = {}
                switches = snap.get("switches")
                if isinstance(switches, list):
                    ps["ebpf.ctx_switch_max"] = f"{max(switches):.0f}" if switches else "0"
                    ps["ebpf.ctx_switch_total"] = f"{sum(switches):.0f}"
                forks = snap.get("forks")
                if isinstance(forks, (int, float)):
                    ps["ebpf.fork_rate"] = f"{forks:.0f}"
                execs = snap.get("execs")
                if isinstance(execs, (int, float)):
                    ps["ebpf.exec_rate"] = f"{execs:.0f}"
                if ps:
                    partial["platform_state"] = ps
            self._cost.record_us(sw.elapsed_us)
            return partial

    # ------------------------------------------------------------------
    def close(self) -> None:
        """Terminate bpftrace subprocess + drain reader thread on shutdown.

        Best-effort: signal cached pgid, wait up to 2s, then SIGKILL.
        Reader thread is joined briefly so it can't publish stale data
        after close() returns.
        """
        self._stopping.set()
        proc = self._proc
        if proc is None:
            return

        def _signal_group(sig: int) -> None:
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
                    log.warning("ebpf_sched: subprocess pid=%s did not exit after SIGKILL", proc.pid)
        finally:
            thread = self._reader_thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)
                if thread.is_alive():
                    log.debug("ebpf_sched: reader thread did not join within 1s")
            self._proc = None
            self._pgid = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


def make() -> EbpfSchedCollector | None:
    from coolstep.compat import caps_if_set
    _c = caps_if_set()
    if _c is not None and not (_c.bpftrace_available or _c.bcc_importable):
        return None
    collector = EbpfSchedCollector()
    return collector if collector.discover() else None
