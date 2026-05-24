"""Unit tests for coolstep.core.hot_state — seqlock mmap state."""

from __future__ import annotations

import math
import threading

from coolstep.core import hot_state


def _state(**overrides):
    base = dict(
        ts_sec=1000.0,
        cpu_temp_now=72.5,
        gpu_temp_max=58.0,
        fan_max_rpm=2400.0,
        throttle_prob=0.12,
        expected_temp_c=74.3,
        busy_ratio_ewma=0.18,
    )
    base.update(overrides)
    return hot_state.HotState(**base)


def test_write_then_read_round_trips(tmp_path, monkeypatch):
    """Writer.write → hot_state.read returns the same scalars."""
    monkeypatch.delenv("RUNTIME_DIRECTORY", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = hot_state.default_path()
    w = hot_state.Writer(path)
    try:
        w.write(_state())
    finally:
        w.close()

    got = hot_state.read(path)
    assert got is not None
    assert got.cpu_temp_now == 72.5
    assert got.gpu_temp_max == 58.0
    assert got.fan_max_rpm == 2400.0
    assert got.throttle_prob == 0.12
    assert got.expected_temp_c == 74.3
    assert got.busy_ratio_ewma == 0.18
    # ts_sec is reconstituted from ts_ns inside the writer — it is the
    # wall-clock at write, NOT the value we passed in. Just sanity-check
    # the type and ordering relative to the input ts.
    assert isinstance(got.ts_sec, float)
    assert got.ts_sec > 0


def test_none_round_trips_as_nan(tmp_path, monkeypatch):
    """None → NaN on the wire → None on read. Critical for nullable
    fields (gpu absent on a CPU-only host, prediction not yet warm)."""
    monkeypatch.delenv("RUNTIME_DIRECTORY", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = hot_state.default_path()
    w = hot_state.Writer(path)
    try:
        w.write(_state(
            cpu_temp_now=70.0,
            gpu_temp_max=None,
            fan_max_rpm=None,
            throttle_prob=None,
            expected_temp_c=None,
            busy_ratio_ewma=0.0,
        ))
    finally:
        w.close()

    got = hot_state.read(path)
    assert got is not None
    assert got.cpu_temp_now == 70.0
    assert got.gpu_temp_max is None
    assert got.fan_max_rpm is None
    assert got.throttle_prob is None
    assert got.expected_temp_c is None
    assert got.busy_ratio_ewma == 0.0


def test_read_absent_file_returns_none(tmp_path, monkeypatch):
    """Daemon not started yet → endpoint must not crash. read() returns None."""
    monkeypatch.delenv("RUNTIME_DIRECTORY", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    # default_path() creates the dir but not the file
    p = hot_state.default_path()
    assert not p.exists()
    assert hot_state.read(p) is None


def test_read_before_first_write_returns_none(tmp_path, monkeypatch):
    """Writer pre-allocated zero-filled file; seq=0 means "no real write yet"
    → reader treats as not-ready instead of returning a phantom record."""
    monkeypatch.delenv("RUNTIME_DIRECTORY", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = hot_state.default_path()
    w = hot_state.Writer(path)
    try:
        assert path.exists()  # writer pre-allocates
        # No write() call yet.
        got = hot_state.read(path)
        assert got is None  # seq=0 should be rejected
    finally:
        w.close()


def test_concurrent_writer_reader_no_torn_frames(tmp_path, monkeypatch):
    """200 reads while a writer churns 1000× — every successful read must
    expose a self-consistent record (seqlock invariant). The point of the
    seqlock is that readers either get a stable frame or None on retry
    exhaustion; they never see a half-written record."""
    monkeypatch.delenv("RUNTIME_DIRECTORY", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = hot_state.default_path()
    w = hot_state.Writer(path)
    stop = threading.Event()

    def writer_loop() -> None:
        i = 0
        while not stop.is_set() and i < 1000:
            # Each write carries a recognisable triple (n, 2n, 3n) so we
            # can verify no field came from a different write generation.
            n = float(i % 100)
            w.write(_state(
                cpu_temp_now=n,
                gpu_temp_max=n * 2.0,
                throttle_prob=n * 3.0 / 1000.0,
            ))
            i += 1

    t = threading.Thread(target=writer_loop, daemon=True)
    t.start()
    try:
        bad = 0
        seen = 0
        for _ in range(200):
            got = hot_state.read(path)
            if got is None:
                continue
            seen += 1
            n = got.cpu_temp_now
            if n is None or math.isnan(n):
                continue
            if got.gpu_temp_max != n * 2.0:
                bad += 1
            if got.throttle_prob is not None and abs(got.throttle_prob - n * 3.0 / 1000.0) > 1e-9:
                bad += 1
        assert seen > 0, "reader saw zero stable frames in 200 attempts"
        assert bad == 0, f"seen {seen}, {bad} frame(s) had cross-generation fields"
    finally:
        stop.set()
        t.join(timeout=2.0)
        w.close()


def test_runtime_dir_falls_back_when_xdg_runtime_dir_missing(monkeypatch, tmp_path):
    """No XDG_RUNTIME_DIR (CI / minimal container) → fallback to
    /tmp/coolstep-$UID. Should not raise."""
    monkeypatch.delenv("RUNTIME_DIRECTORY", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    # We don't assert the exact path because $TMPDIR / /tmp differ per host;
    # the contract is "writable, mode 0700, doesn't raise".
    d = hot_state.runtime_dir()
    assert d.exists()
    assert d.is_dir()


def test_runtime_dir_prefers_systemd_runtime_directory(monkeypatch, tmp_path):
    """$RUNTIME_DIRECTORY (systemd-managed) wins over XDG_RUNTIME_DIR.
    Path is returned as-is — systemd guaranteed its creation/mode."""
    sd_path = tmp_path / "sd-runtime"
    sd_path.mkdir(mode=0o700)
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(sd_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "should-be-ignored"))
    assert hot_state.runtime_dir() == sd_path
