"""Workload profile resolver — P2.5.

The pre-P2.5 curve had one flat list of «known-hot» workload classes that
got a uniform +5% knee bump. Reality is coarser-but-richer: a Code session
should be calmer than the default, a Render session warmer, a Game session
warmer still. This module is the single source of truth for that grouping.

`is_headroom_class()` is the matcher curve.py reuses for `workload_headroom`,
so headroom and profile resolution can't drift apart on prefix-wrappers
(steam_app_*, lutris-*, heroic-*).

Resolution order: explicit `workload_class` (from hyprctl focused window)
beats anything we'd guess from `top_processes`. Top-process fallback only
fires when the focused-window signal is missing — typical for headless or
just-after-boot states.
"""

from __future__ import annotations

from enum import StrEnum


class Profile(StrEnum):
    CODE = "code"
    RENDER = "render"
    GAME = "game"
    BROWSER = "browser"
    IDLE = "idle"
    OTHER = "other"


CODE_CLASSES: frozenset[str] = frozenset({
    "kitty", "alacritty", "foot", "wezterm", "code", "vscode",
    "neovide",
})
RENDER_CLASSES: frozenset[str] = frozenset({
    "blender", "kdenlive", "obs", "obs-studio", "ffmpeg",
    "darktable", "gimp", "krita",
})
GAME_CLASSES: frozenset[str] = frozenset({
    "steam", "csgo_linux64", "csgo", "wine64-preloader",
    "wineserver", "gamescope",
})
# Modern desktop browsers — stable hot-ish workload (WebGL / video decode /
# JS JIT). Empirically the dominant thermal source on this target (~59 %
# of throttle events were on `zen`), so it gets its own bucket and a small
# proactive bump rather than the CODE −2 % calm bias it used to inherit.
BROWSER_CLASSES: frozenset[str] = frozenset({
    "zen", "firefox", "firefox-developer-edition", "librewolf",
    "chromium", "chromium-browser", "google-chrome", "brave",
    "brave-browser", "vivaldi", "qutebrowser",
})
# Wrapper prefixes that Hyprland / window-managers emit per launched title:
#   steam_app_<appid>     — every Steam game (this was the silent gap)
#   lutris-<game>         — Lutris launches
#   heroic-<game>         — Heroic Games Launcher (Epic / GOG)
GAME_CLASS_PREFIXES: tuple[str, ...] = (
    "steam_app_", "lutris-", "heroic-",
)
IDLE_CLASSES: frozenset[str] = frozenset({
    "swaylock", "hyprlock", "wlogout",
})


def _classify(name: str) -> Profile | None:
    """Lower-cased name → Profile, or None if no match."""
    if name in GAME_CLASSES or name.startswith(GAME_CLASS_PREFIXES):
        return Profile.GAME
    if name in RENDER_CLASSES:
        return Profile.RENDER
    if name in BROWSER_CLASSES:
        return Profile.BROWSER
    if name in IDLE_CLASSES:
        return Profile.IDLE
    if name in CODE_CLASSES:
        return Profile.CODE
    return None


def is_headroom_class(name: str) -> bool:
    """True for any class that resolves to Game / Render / Browser.

    Single matcher shared with `_classify` so prefix-wrappers (steam_app_*,
    lutris-*, heroic-*) stay headroom-eligible. Used by `workload_headroom`
    in curve.py — gating the +5 % knee bump for known-hot launches."""
    p = _classify(name.lower())
    return p in (Profile.GAME, Profile.RENDER, Profile.BROWSER)


# Fallback priority order — when more than one profile matches the
# top_processes list, the cooling system answers to the more aggressive
# one. A terminal next to Steam is a Game scene; Blender next to a game
# is still a Game scene (gamescope wraps render workloads).
_FALLBACK_PRIORITY: tuple[Profile, ...] = (
    Profile.GAME,
    Profile.RENDER,
    Profile.BROWSER,
    Profile.IDLE,
    Profile.CODE,
)


def resolve_profile(
    workload_class: str | None,
    top_processes: tuple[str, ...] | list[str] = (),
) -> Profile:
    """Decide the active profile.

    Both the focused window class and the top CPU-consuming processes are
    contributors — the highest-priority match across all of them wins
    (`_FALLBACK_PRIORITY`). This catches the everyday case where a heavy
    workload (game, render, build) is in the background while the user
    is focused on a terminal or browser: we cool for the workload that's
    actually burning CPU, not the one in the foreground."""
    candidates: set[Profile] = set()
    if workload_class:
        hit = _classify(workload_class.lower())
        if hit is not None:
            candidates.add(hit)
    for proc in top_processes:
        if not proc:
            continue
        hit = _classify(proc.lower())
        if hit is not None:
            candidates.add(hit)
    for prio in _FALLBACK_PRIORITY:
        if prio in candidates:
            return prio
    return Profile.OTHER
