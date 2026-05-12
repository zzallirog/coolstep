"""Workload profile resolver — P2.5.

The pre-P2.5 curve had one flat list of «known-hot» workload classes that
got a uniform +5% knee bump. Reality is coarser-but-richer: a Code session
should be calmer than the default, a Render session warmer, a Game session
warmer still. This module is the single source of truth for that grouping.

`WORKLOAD_CLASSES_HEADROOM` in curve.py is derived from `RENDER_CLASSES |
GAME_CLASSES` so the two lists can't drift.

Resolution order: explicit `workload_class` (from hyprctl focused window)
beats anything we'd guess from `top_processes`. Top-process fallback only
fires when the focused-window signal is missing — typical for headless or
just-after-boot states.
"""

from __future__ import annotations

from enum import Enum


class Profile(str, Enum):
    CODE = "code"
    RENDER = "render"
    GAME = "game"
    IDLE = "idle"
    OTHER = "other"


CODE_CLASSES: frozenset[str] = frozenset({
    "kitty", "alacritty", "foot", "wezterm", "code", "vscode",
    "zen", "firefox-developer-edition", "qutebrowser", "neovide",
})
RENDER_CLASSES: frozenset[str] = frozenset({
    "blender", "kdenlive", "obs", "obs-studio", "ffmpeg",
    "darktable", "gimp", "krita",
})
GAME_CLASSES: frozenset[str] = frozenset({
    "steam", "csgo_linux64", "csgo", "wine64-preloader",
    "wineserver", "gamescope",
})
IDLE_CLASSES: frozenset[str] = frozenset({
    "swaylock", "hyprlock", "wlogout",
})


def _classify(name: str) -> Profile | None:
    """Lower-cased name → Profile, or None if no match."""
    if name in GAME_CLASSES:
        return Profile.GAME
    if name in RENDER_CLASSES:
        return Profile.RENDER
    if name in IDLE_CLASSES:
        return Profile.IDLE
    if name in CODE_CLASSES:
        return Profile.CODE
    return None


# Fallback priority order — when more than one profile matches the
# top_processes list, the cooling system answers to the more aggressive
# one. A terminal next to Steam is a Game scene; Blender next to a game
# is still a Game scene (gamescope wraps render workloads).
_FALLBACK_PRIORITY: tuple[Profile, ...] = (
    Profile.GAME,
    Profile.RENDER,
    Profile.IDLE,
    Profile.CODE,
)


def resolve_profile(
    workload_class: str | None,
    top_processes: tuple[str, ...] | list[str] = (),
) -> Profile:
    """Decide the active profile.

    Focused window class wins outright when it classifies. The top_processes
    fallback collects all matches then picks the highest-priority one — see
    `_FALLBACK_PRIORITY`.
    """
    if workload_class:
        hit = _classify(workload_class.lower())
        if hit is not None:
            return hit
    seen: set[Profile] = set()
    for proc in top_processes:
        if not proc:
            continue
        hit = _classify(proc.lower())
        if hit is not None:
            seen.add(hit)
    for prio in _FALLBACK_PRIORITY:
        if prio in seen:
            return prio
    return Profile.OTHER
