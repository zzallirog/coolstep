"""Workload profile resolver — P2.5."""

from __future__ import annotations

from coolstep.core.workload_profile import (
    CODE_CLASSES,
    GAME_CLASSES,
    IDLE_CLASSES,
    RENDER_CLASSES,
    Profile,
    resolve_profile,
)


def test_resolve_code_from_focused_class():
    assert resolve_profile("kitty") is Profile.CODE
    assert resolve_profile("vscode") is Profile.CODE


def test_resolve_render_from_focused_class():
    assert resolve_profile("blender") is Profile.RENDER
    assert resolve_profile("obs-studio") is Profile.RENDER


def test_resolve_game_from_focused_class():
    assert resolve_profile("steam") is Profile.GAME
    assert resolve_profile("csgo_linux64") is Profile.GAME


def test_resolve_idle_from_focused_class():
    assert resolve_profile("hyprlock") is Profile.IDLE


def test_resolve_other_for_unknown_class():
    assert resolve_profile("some-random-app") is Profile.OTHER


def test_resolve_other_when_workload_class_none_and_no_procs():
    assert resolve_profile(None) is Profile.OTHER
    assert resolve_profile(None, ()) is Profile.OTHER


def test_resolve_falls_back_to_top_processes():
    # Focused window unknown but Steam runs in top procs → GAME.
    assert resolve_profile(None, ["systemd", "kthread", "steam"]) is Profile.GAME


def test_resolve_focused_class_beats_top_processes():
    """Explicit workload_class trumps top_processes when it classifies."""
    assert resolve_profile("blender", ["steam"]) is Profile.RENDER


def test_resolve_case_insensitive():
    assert resolve_profile("KITTY") is Profile.CODE
    assert resolve_profile("Blender") is Profile.RENDER


def test_resolve_skips_empty_proc_names():
    assert resolve_profile(None, ["", "", "kitty"]) is Profile.CODE


def test_resolve_priority_game_over_render_over_idle_over_code_in_fallback():
    """In top_processes fallback, more-aggressive profile wins ties.
    Order: GAME > RENDER > IDLE > CODE."""
    # game beats render
    assert resolve_profile(None, ["blender", "steam"]) is Profile.GAME
    # render beats code
    assert resolve_profile(None, ["kitty", "blender"]) is Profile.RENDER
    # idle beats code (when only those two present)
    assert resolve_profile(None, ["kitty", "hyprlock"]) is Profile.IDLE


def test_frozensets_are_disjoint():
    """Sanity — a class can only belong to one profile bucket."""
    assert CODE_CLASSES.isdisjoint(RENDER_CLASSES)
    assert CODE_CLASSES.isdisjoint(GAME_CLASSES)
    assert CODE_CLASSES.isdisjoint(IDLE_CLASSES)
    assert RENDER_CLASSES.isdisjoint(GAME_CLASSES)
    assert RENDER_CLASSES.isdisjoint(IDLE_CLASSES)
    assert GAME_CLASSES.isdisjoint(IDLE_CLASSES)


def test_profile_str_values():
    assert Profile.CODE.value == "code"
    assert Profile.RENDER.value == "render"
    assert Profile.GAME.value == "game"
    assert Profile.IDLE.value == "idle"
    assert Profile.OTHER.value == "other"
