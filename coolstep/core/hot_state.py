"""Seqlock-style mmap hot state for the live-telemetry path.

Balance-plan step III. Removes `ml-state.json` from the per-tick hot path:
the daemon writes a fixed 64-byte struct in tmpfs (`$XDG_RUNTIME_DIR/coolstep/state.mmap`)
on every tick, dashboard `/api/telemetry/latest` reads it. tmpfs keeps the
page RAM-resident, no disk I/O — `dump_ml_state` was the dominant tick
stage (~200-2000ms under swap pressure); reduces to <0.1ms here.

Layout (little-endian, fixed 64 bytes):
    seq:   uint64   — even = stable; odd = write in progress
    ts_ns: uint64   — time.time_ns() at write
    body:  6 × double — packed scalars (see HotState dataclass)

Concurrency: one writer (daemon), many readers (dashboard workers).
Writer pattern (kernel `seqcount_t` idiom):
    seq+=1 (odd) → write ts+body → seq+=1 (even)
Reader pattern (retry until stable):
    s1 = seq; read body; s2 = seq; valid iff s1==s2 and s1 is even
No locks, no syscalls on the read path.

tmpfs caveat: state is wiped at logout. That is fine — this is "what is
happening NOW", not durable state. `ml-state.json` continues to be the
periodic full snapshot.
"""

from __future__ import annotations

import mmap
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path

# Header: seq (u64) + ts_ns (u64) = 16 bytes
_HEADER = struct.Struct("<QQ")
# Body: 6 doubles = 48 bytes. Keep field order in sync with HotState below.
_BODY = struct.Struct("<dddddd")
_RECORD_SIZE = 64  # power-of-2 for cache-line alignment; HEADER+BODY = 64

assert _HEADER.size + _BODY.size == _RECORD_SIZE


@dataclass
class HotState:
    """Snapshot consumed by the live-telemetry tile.

    All fields are scalar doubles to keep the binary layout fixed.
    None-valued sources are encoded as NaN at write time and decoded
    back to None on read.
    """

    ts_sec: float
    cpu_temp_now: float | None
    gpu_temp_max: float | None
    fan_max_rpm: float | None
    throttle_prob: float | None
    expected_temp_c: float | None
    busy_ratio_ewma: float | None


def runtime_dir() -> Path:
    """Resolve the per-user tmpfs path for coolstep hot state.

    Resolution order:
      1. $RUNTIME_DIRECTORY  — set by systemd `RuntimeDirectory=coolstep`
         (writable bind-mount, even under ProtectSystem=strict).
      2. $XDG_RUNTIME_DIR/coolstep — standalone runs (CLI, tests).
      3. /tmp/coolstep-$UID — fallback when neither is set.
    """
    runtime = os.environ.get("RUNTIME_DIRECTORY")
    if runtime:
        # systemd guarantees this path exists with the right mode; just use it.
        return Path(runtime)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        sub = Path(xdg) / "coolstep"
    else:
        sub = Path("/tmp") / f"coolstep-{os.getuid()}"
    sub.mkdir(parents=True, exist_ok=True, mode=0o700)
    return sub


def default_path() -> Path:
    return runtime_dir() / "state.mmap"


def _to_nan(v: float | None) -> float:
    return float("nan") if v is None else float(v)


def _from_nan(v: float) -> float | None:
    # NaN != NaN — the canonical Python check.
    return None if v != v else v


class Writer:
    """Single-writer mmap handle. Daemon owns one instance per process."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()
        if not self.path.exists():
            # Pre-allocate the file at the exact record size. mmap on
            # Linux requires the underlying file to be ≥ the map length.
            with open(self.path, "wb") as f:
                f.write(b"\x00" * _RECORD_SIZE)
            os.chmod(self.path, 0o600)
        self._fd = os.open(self.path, os.O_RDWR)
        self._mm = mmap.mmap(self._fd, _RECORD_SIZE)
        self._seq = 0

    def write(self, state: HotState) -> None:
        """Atomic update of the record. O(1), no syscalls.

        Two header writes around the body write form a seqlock — readers
        detect a torn frame by seq-mismatch and retry.
        """
        # Step 1: bump to odd (write in progress).
        self._seq += 1
        self._mm[:_HEADER.size] = _HEADER.pack(self._seq, time.time_ns())
        # Step 2: body.
        _BODY.pack_into(
            self._mm, _HEADER.size,
            _to_nan(state.cpu_temp_now),
            _to_nan(state.gpu_temp_max),
            _to_nan(state.fan_max_rpm),
            _to_nan(state.throttle_prob),
            _to_nan(state.expected_temp_c),
            _to_nan(state.busy_ratio_ewma),
        )
        # Step 3: bump to even (stable).
        self._seq += 1
        self._mm[:_HEADER.size] = _HEADER.pack(self._seq, time.time_ns())

    def close(self) -> None:
        try:
            self._mm.close()
        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass


def read(path: Path | None = None, retries: int = 8) -> HotState | None:
    """Lock-free read. Returns None when the file is absent or every retry
    saw a torn frame (writer was contending; rare at 5-10Hz)."""
    p = path or default_path()
    if not p.exists():
        return None
    try:
        with open(p, "rb") as f:
            with mmap.mmap(f.fileno(), _RECORD_SIZE, access=mmap.ACCESS_READ) as m:
                for _ in range(retries):
                    seq1, ts_ns = _HEADER.unpack(m[:_HEADER.size])
                    body = _BODY.unpack(m[_HEADER.size:_RECORD_SIZE])
                    seq2, _ts2 = _HEADER.unpack(m[:_HEADER.size])
                    if seq1 == seq2 and seq1 % 2 == 0 and seq1 > 0:
                        return HotState(
                            ts_sec=ts_ns / 1_000_000_000.0,
                            cpu_temp_now=_from_nan(body[0]),
                            gpu_temp_max=_from_nan(body[1]),
                            fan_max_rpm=_from_nan(body[2]),
                            throttle_prob=_from_nan(body[3]),
                            expected_temp_c=_from_nan(body[4]),
                            busy_ratio_ewma=_from_nan(body[5]),
                        )
    except (OSError, struct.error, ValueError):
        return None
    return None
