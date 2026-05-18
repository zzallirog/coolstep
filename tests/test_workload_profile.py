"""Workload profile resolver — P2.5."""

from __future__ import annotations

from coolstep.core.workload_profile import (
    BROWSER_CLASSES,
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


def test_resolve_game_from_steam_app_prefix():
    # Hyprland window class for every Steam game.
    assert resolve_profile("steam_app_2073850") is Profile.GAME  # THE FINALS
    assert resolve_profile("STEAM_APP_440") is Profile.GAME  # case-insensitive
    # Fallback via top_processes carries the same matcher.
    assert resolve_profile(None, ["steam_app_730"]) is Profile.GAME


def test_resolve_game_from_lutris_heroic_prefix():
    assert resolve_profile("lutris-witcher3") is Profile.GAME
    assert resolve_profile("heroic-cyberpunk2077") is Profile.GAME


def test_resolve_browser_from_focused_class():
    # zen used to inherit CODE -2 % calm bias despite being the dominant
    # heat source — moved to its own bucket.
    assert resolve_profile("zen") is Profile.BROWSER
    assert resolve_profile("firefox") is Profile.BROWSER
    assert resolve_profile("chromium") is Profile.BROWSER
    assert resolve_profile("FIREFOX") is Profile.BROWSER  # case-insensitive


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


def test_resolve_heavy_background_beats_focused():
    """Highest-priority profile across focused + top_processes wins.

    Heavy background workload (steam game, blender) should drive cooling
    even when the user is focused on a terminal or browser — the chip
    doesn't care which window has keyboard focus."""
    # focused=kitty (CODE), bg=steam → GAME wins
    assert resolve_profile("kitty", ["steam"]) is Profile.GAME
    # focused=zen (BROWSER), bg=blender → RENDER wins
    assert resolve_profile("zen", ["blender"]) is Profile.RENDER
    # focused=blender (RENDER), bg=steam → GAME wins (game > render)
    assert resolve_profile("blender", ["steam"]) is Profile.GAME
    # focused=steam_app_*, bg=anything quieter → still GAME (focused already top)
    assert resolve_profile("steam_app_2073850", ["kitty"]) is Profile.GAME


def test_resolve_focused_wins_when_no_bg_contributor():
    """When top_processes has nothing classifiable, focused is the only vote."""
    assert resolve_profile("blender", ["systemd", "kthread"]) is Profile.RENDER
    assert resolve_profile("zen", []) is Profile.BROWSER


def test_resolve_case_insensitive():
    assert resolve_profile("KITTY") is Profile.CODE
    assert resolve_profile("Blender") is Profile.RENDER


def test_resolve_skips_empty_proc_names():
    assert resolve_profile(None, ["", "", "kitty"]) is Profile.CODE


def test_resolve_priority_game_over_render_over_browser_over_idle_over_code_in_fallback():
    """In top_processes fallback, more-aggressive profile wins ties.
    Order: GAME > RENDER > BROWSER > IDLE > CODE."""
    # game beats render
    assert resolve_profile(None, ["blender", "steam"]) is Profile.GAME
    # render beats code
    assert resolve_profile(None, ["kitty", "blender"]) is Profile.RENDER
    # render beats browser
    assert resolve_profile(None, ["zen", "blender"]) is Profile.RENDER
    # browser beats code
    assert resolve_profile(None, ["kitty", "zen"]) is Profile.BROWSER
    # idle beats code (when only those two present)
    assert resolve_profile(None, ["kitty", "hyprlock"]) is Profile.IDLE


def test_frozensets_are_disjoint():
    """Sanity — a class can only belong to one profile bucket."""
    buckets = (CODE_CLASSES, RENDER_CLASSES, GAME_CLASSES, BROWSER_CLASSES, IDLE_CLASSES)
    for i, a in enumerate(buckets):
        for b in buckets[i+1:]:
            assert a.isdisjoint(b), f"overlap: {a & b}"


def test_profile_str_values():
    assert Profile.BROWSER.value == "browser"
    assert Profile.CODE.value == "code"
    assert Profile.RENDER.value == "render"
    assert Profile.GAME.value == "game"
    assert Profile.IDLE.value == "idle"
    assert Profile.OTHER.value == "other"
