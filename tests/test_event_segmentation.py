"""Tests for the EventSegmenter state machine — Phase G (Heavy 3)."""

from __future__ import annotations

from coolstep.core.event_segmentation import EventSegmenter, SessionBoundary
from coolstep.core.schema import CpuMetrics, TelemetryFrame, WorkloadFrame


def _frame(
    ts: float,
    *,
    load: float = 30.0,
    tctl: float = 70.0,
    label: str | None = "code",
) -> TelemetryFrame:
    workload = WorkloadFrame(label=label) if label is not None else WorkloadFrame()
    return TelemetryFrame(
        timestamp=ts,
        cpu=CpuMetrics(load_pct=[load, load], temps_c={"tctl": tctl}),
        workload=workload,
    )


# ── initial / continuation ────────────────────────────────────────────


def test_first_frame_emits_initial_boundary():
    seg = EventSegmenter()
    b = seg.feed(_frame(100.0))
    assert isinstance(b, SessionBoundary)
    assert b.reason == "initial"
    assert b.prev_session_id is None
    assert b.new_session_id.endswith("-i")
    assert seg.current_session_id() == b.new_session_id


def test_steady_stream_keeps_same_session_id():
    seg = EventSegmenter(plateau_window=999)  # disable plateau folding
    first = seg.feed(_frame(100.0, load=30.0))
    assert first is not None
    # Many in-tolerance frames — no boundary should fire.
    for i in range(1, 10):
        b = seg.feed(_frame(100.0 + i, load=30.0 + 0.5 * (i % 2), tctl=70.0))
        assert b is None
    assert seg.current_session_id() == first.new_session_id


# ── focus_change ──────────────────────────────────────────────────────


def test_focus_change_triggers_new_session():
    seg = EventSegmenter()
    a = seg.feed(_frame(100.0, label="code"))
    assert a is not None
    none = seg.feed(_frame(101.0, label="code"))
    assert none is None
    b = seg.feed(_frame(102.0, label="game"))
    assert b is not None
    assert b.reason == "focus_change"
    assert b.prev_session_id == a.new_session_id
    assert b.new_session_id.endswith("-f")


def test_focus_change_none_to_label_is_a_change():
    """Switching from no workload info to a labelled one is a focus
    change — the user just started doing something namable."""
    seg = EventSegmenter()
    seg.feed(_frame(100.0, label=None))
    b = seg.feed(_frame(101.0, label="render"))
    assert b is not None
    assert b.reason == "focus_change"


# ── load_jump ─────────────────────────────────────────────────────────


def test_load_jump_triggers_new_session():
    seg = EventSegmenter(load_jump_threshold_pct=25.0)
    a = seg.feed(_frame(100.0, load=20.0))
    assert a is not None
    none = seg.feed(_frame(101.0, load=22.0))  # +2 — under threshold
    assert none is None
    b = seg.feed(_frame(102.0, load=80.0))     # +58 — over threshold
    assert b is not None
    assert b.reason == "load_jump"
    assert b.new_session_id.endswith("-j")
    assert b.prev_session_id == a.new_session_id


def test_load_jump_threshold_respects_param():
    seg = EventSegmenter(load_jump_threshold_pct=50.0)
    seg.feed(_frame(100.0, load=20.0))
    # +30 — would trigger at default 25, but param says 50 so no.
    none = seg.feed(_frame(101.0, load=50.0))
    assert none is None


# ── plateau folding ───────────────────────────────────────────────────


def test_plateau_folds_60_in_tolerance_frames_into_one_session():
    """The whole point of plateau detection: a long run of similar
    frames yields ONE session id, not 60 separate ones."""
    seg = EventSegmenter(
        plateau_window=30,
        plateau_load_tolerance_pct=5.0,
        plateau_temp_tolerance_c=1.0,
    )
    first = seg.feed(_frame(0.0, load=50.0, tctl=75.0))
    assert first is not None
    session_id = first.new_session_id

    boundaries: list[SessionBoundary] = []
    for i in range(1, 61):
        # Stay within ±2 % load and ±0.5 °C — comfortably inside plateau
        # tolerance, no load_jump (Δ ≪ 25 pp).
        b = seg.feed(_frame(
            float(i),
            load=50.0 + 2.0 * ((i % 2) - 0.5),
            tctl=75.0 + 0.5 * ((i % 2) - 0.5),
            label="code",
        ))
        if b is not None:
            boundaries.append(b)

    # No new sessions should have spawned — we never broke tolerance.
    assert boundaries == []
    assert seg.current_session_id() == session_id


def test_plateau_breaks_correctly_after_tolerance_violation():
    """Once a plateau has formed, the FIRST frame outside tolerance must
    open a new session with reason=plateau_collapse."""
    seg = EventSegmenter(
        plateau_window=10,
        plateau_load_tolerance_pct=5.0,
        plateau_temp_tolerance_c=1.0,
    )
    first = seg.feed(_frame(0.0, load=50.0, tctl=75.0))
    assert first is not None

    # Fill the trailing window with steady frames so plateau is reached.
    for i in range(1, 15):
        b = seg.feed(_frame(float(i), load=50.5, tctl=75.2, label="code"))
        assert b is None

    # The next frame breaks the TEMP plateau (Δ=+3 °C), but holds load
    # within both the 5-pp plateau-load tolerance AND the 25-pp
    # load_jump threshold — so the trigger we exercise is purely the
    # plateau collapse, not a load_jump masquerade.
    b = seg.feed(_frame(15.0, load=50.5, tctl=78.5, label="code"))
    assert b is not None
    assert b.reason == "plateau_collapse"
    assert b.new_session_id.endswith("-p")
    assert b.prev_session_id == first.new_session_id


def test_plateau_does_not_fire_before_window_is_full():
    """Plateau requires N trailing frames; until the window fills, a
    temp drift just continues the initial session."""
    seg = EventSegmenter(plateau_window=30)
    seg.feed(_frame(0.0, load=50.0, tctl=75.0))
    # Only 5 frames of stable state — well below the 30-frame window.
    for i in range(1, 6):
        b = seg.feed(_frame(float(i), load=50.5, tctl=75.2, label="code"))
        assert b is None
    # A modest temp drift (still within load_jump threshold AND no
    # focus change) doesn't fire — plateau was never declared, so it
    # can't collapse.
    b = seg.feed(_frame(6.0, load=50.5, tctl=80.0, label="code"))
    assert b is None


# ── id format ─────────────────────────────────────────────────────────


def test_session_id_format():
    seg = EventSegmenter()
    a = seg.feed(_frame(1700000000.5))
    assert a is not None
    # int(ts) for cross-stream comparability, reason letter from map.
    assert a.new_session_id == "1700000000-i"


def test_current_session_id_before_feed_raises():
    seg = EventSegmenter()
    try:
        seg.current_session_id()
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError before any feed()")


# ── interaction between rules ─────────────────────────────────────────


def test_focus_change_wins_over_load_jump_in_same_frame():
    """If both fire on the same frame, focus_change is checked first —
    label is the higher-signal trigger («I switched apps»)."""
    seg = EventSegmenter(load_jump_threshold_pct=25.0)
    seg.feed(_frame(0.0, load=10.0, label="code"))
    b = seg.feed(_frame(1.0, load=90.0, label="game"))
    assert b is not None
    assert b.reason == "focus_change"


def test_plateau_state_resets_after_any_boundary():
    """After a boundary fires for any reason, the new session must
    rebuild its plateau window from scratch."""
    seg = EventSegmenter(plateau_window=5, plateau_load_tolerance_pct=5.0,
                         plateau_temp_tolerance_c=1.0)
    seg.feed(_frame(0.0, load=30.0, tctl=70.0))
    for i in range(1, 7):
        seg.feed(_frame(float(i), load=30.5, tctl=70.2, label="code"))
    # plateau now formed — force a focus_change boundary
    b = seg.feed(_frame(7.0, load=30.5, tctl=70.2, label="render"))
    assert b is not None and b.reason == "focus_change"
    # Immediately after the boundary, the new session has only 1 frame
    # in its window — no plateau collapse should be possible yet.
    b2 = seg.feed(_frame(8.0, load=30.5, tctl=75.0, label="render"))
    assert b2 is None
