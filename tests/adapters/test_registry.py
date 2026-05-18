"""Collector registry — discover()."""

from __future__ import annotations

from coolstep.adapters.collectors import discover


def test_discover_returns_list():
    found = discover()
    assert isinstance(found, list)
    # На Linux хосте linux_sysfs всегда обнаружится — это smoke
    names = [c.name for c in found]
    assert "linux_sysfs" in names
