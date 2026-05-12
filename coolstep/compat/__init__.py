"""coolstep.compat — platform compatibility layer.

Public API:
    get_caps() → PlatformCaps    lazy singleton, safe to call many times
    reset_caps()                 force re-detection (useful in tests)

The singleton is populated on first call to get_caps().  Subsequent calls
return the cached instance — detection is I/O-heavy (~50 ms on first run).

Pattern for adapters:
    def make() -> MyCollector | None:
        from coolstep.compat import get_caps
        if not get_caps().some_capability:
            return None
        ...
"""

from __future__ import annotations

from coolstep.compat.caps import PlatformCaps

_caps: PlatformCaps | None = None


def get_caps() -> PlatformCaps:
    """Return platform caps, detecting once and caching for the process lifetime.

    Prefer calling this from daemon startup code or the CLI. Adapter make()
    functions should use caps_if_set() instead so that test mocks are not
    bypassed by a stale singleton.
    """
    global _caps
    if _caps is None:
        from coolstep.compat.detect import detect_caps
        _caps = detect_caps()
    return _caps


def caps_if_set() -> PlatformCaps | None:
    """Return caps only if already detected/injected; None otherwise.

    Use this in adapter make() functions as an optional fast early-return:

        caps = caps_if_set()
        if caps is not None and not caps.some_flag:
            return None   # fast path — caps says this adapter is irrelevant

    When caps is None (e.g. in unit tests that don't pre-detect), the check is
    skipped and the adapter falls through to its own runtime discovery checks.
    This keeps adapter tests working without needing to stub PlatformCaps.
    """
    return _caps


def reset_caps(caps: PlatformCaps | None = None) -> None:
    """Replace the cached caps.  Pass None to force re-detection on next call.

    Used in tests:
        reset_caps(PlatformCaps(...))   # inject a fake caps
        reset_caps()                    # clear cache; next get_caps() re-detects
    """
    global _caps
    _caps = caps


from coolstep.compat.manifest import (
    deep_merge,
    load_manifest,
    manifest_paths,
    reset_manifest,
)

__all__ = [
    "PlatformCaps",
    "caps_if_set",
    "deep_merge",
    "get_caps",
    "load_manifest",
    "manifest_paths",
    "reset_caps",
    "reset_manifest",
]
