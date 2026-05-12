"""Tests for the three-layer manifest topology (L0/L1/L2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coolstep.compat.manifest import (
    deep_merge,
    load_manifest,
    reset_manifest,
)


@pytest.fixture(autouse=True)
def _clear_manifest():
    reset_manifest(None)
    yield
    reset_manifest(None)


# ---------------------------------------------------------------------------
# Deep merge primitive


class TestDeepMerge:
    def test_dict_recursive(self):
        base = {"a": 1, "b": {"c": 2, "d": 3}}
        overlay = {"b": {"d": 99, "e": 4}}
        result = deep_merge(base, overlay)
        assert result == {"a": 1, "b": {"c": 2, "d": 99, "e": 4}}

    def test_overlay_replaces_scalars(self):
        assert deep_merge(1, 2) == 2
        assert deep_merge("a", "b") == "b"

    def test_list_replaces_by_default(self):
        base = {"x": [1, 2, 3]}
        overlay = {"x": [9, 8]}
        assert deep_merge(base, overlay) == {"x": [9, 8]}

    def test_disable_directive(self):
        base = {"a": 1, "b": 2}
        overlay = {"b": {"_disable": True}}
        result = deep_merge(base, overlay)
        assert "b" not in result
        assert result == {"a": 1}

    def test_id_list_deep_merge(self):
        base = [
            {"id": "x", "trigger": "t1", "reason": "r1", "commands": {"arch": "old"}},
            {"id": "y", "trigger": "t2"},
        ]
        overlay = [
            {"id": "x", "commands": {"arch": "new", "debian": "added"}},
        ]
        result = deep_merge(base, overlay)
        x = next(i for i in result if i["id"] == "x")
        # commands deep-merged
        assert x["commands"]["arch"] == "new"
        assert x["commands"]["debian"] == "added"
        # Other fields preserved
        assert x["trigger"] == "t1"
        assert x["reason"] == "r1"
        # y untouched
        assert any(i.get("id") == "y" for i in result)

    def test_id_list_remove(self):
        base = [{"id": "a"}, {"id": "b"}]
        overlay = [{"id": "a", "_remove": True}]
        result = deep_merge(base, overlay)
        ids = {i.get("id") for i in result}
        assert "a" not in ids
        assert "b" in ids

    def test_id_list_new_item_appended(self):
        base = [{"id": "a"}]
        overlay = [{"id": "b", "extra": 1}]
        result = deep_merge(base, overlay)
        ids = {i.get("id") for i in result}
        assert ids == {"a", "b"}

    def test_nested_override_preserves_unrelated(self):
        base = {"hwmon": {"cpu": ["a", "b"], "fan": ["c"]}}
        overlay = {"hwmon": {"fan": ["d", "e"]}}
        result = deep_merge(base, overlay)
        # CPU list preserved, fan replaced
        assert result["hwmon"]["cpu"] == ["a", "b"]
        assert result["hwmon"]["fan"] == ["d", "e"]


# ---------------------------------------------------------------------------
# Manifest loading


class TestLoadManifest:
    def test_core_loads(self):
        """The bundled core.json must always be loadable."""
        m = load_manifest()
        assert "hwmon" in m
        assert "cpu_drivers" in m["hwmon"]
        assert "k10temp" in m["hwmon"]["cpu_drivers"]
        assert "gpu_vendors" in m
        assert m["gpu_vendors"]["amd"] == "0x1002"

    def test_distro_mapping_complete(self):
        m = load_manifest()
        id_to_clan = m["distro"]["id_to_clan"]
        # Common distros
        assert id_to_clan["arch"] == "arch"
        assert id_to_clan["ubuntu"] == "debian"
        assert id_to_clan["fedora"] == "fedora"

    def test_community_pointers_have_ids(self):
        m = load_manifest()
        pointers = m["community_pointers"]
        # Every pointer must have an id (for L1/L2 id-based merge)
        for p in pointers:
            assert "id" in p, f"pointer missing id: {p}"
            assert "commands" in p, f"pointer missing commands: {p['id']}"


class TestL2Override:
    def test_custom_extends_cpu_drivers(self, monkeypatch, tmp_path: Path):
        # Point XDG_CONFIG_HOME at a tmp dir with a custom.json
        custom_dir = tmp_path / "coolstep"
        custom_dir.mkdir()
        custom = {
            "hwmon": {
                "cpu_drivers": ["k10temp", "coretemp", "my_custom_chip"]
            }
        }
        (custom_dir / "custom.json").write_text(json.dumps(custom))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        reset_manifest(None)
        m = load_manifest()
        # L2 replaces the cpu_drivers list (lists are replaced, not merged)
        assert "my_custom_chip" in m["hwmon"]["cpu_drivers"]

    def test_custom_disables_pointer(self, monkeypatch, tmp_path: Path):
        custom_dir = tmp_path / "coolstep"
        custom_dir.mkdir()
        custom = {
            "community_pointers": [
                {"id": "bpftrace", "_remove": True}
            ]
        }
        (custom_dir / "custom.json").write_text(json.dumps(custom))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        reset_manifest(None)
        m = load_manifest()
        ids = {p.get("id") for p in m["community_pointers"]}
        assert "bpftrace" not in ids
        # Other pointers remain
        assert "asusctl" in ids

    def test_custom_overrides_command(self, monkeypatch, tmp_path: Path):
        custom_dir = tmp_path / "coolstep"
        custom_dir.mkdir()
        custom = {
            "community_pointers": [
                {"id": "bpftrace", "commands": {"arch": "MY_CUSTOM_INSTALL_CMD"}}
            ]
        }
        (custom_dir / "custom.json").write_text(json.dumps(custom))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        reset_manifest(None)
        m = load_manifest()
        bpftrace_p = next(p for p in m["community_pointers"] if p.get("id") == "bpftrace")
        assert bpftrace_p["commands"]["arch"] == "MY_CUSTOM_INSTALL_CMD"
        # Other distros preserved from L0
        assert "apt" in bpftrace_p["commands"].get("debian", "")

    def test_corrupt_custom_skipped_no_fail(self, monkeypatch, tmp_path: Path, caplog):
        custom_dir = tmp_path / "coolstep"
        custom_dir.mkdir()
        (custom_dir / "custom.json").write_text("{ this is not valid json")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        reset_manifest(None)
        # Should not raise
        m = load_manifest()
        # Core still loaded
        assert "hwmon" in m


class TestSingleton:
    def test_cached_after_first_load(self):
        a = load_manifest()
        b = load_manifest()
        assert a is b

    def test_reset_forces_reload(self):
        a = load_manifest()
        reset_manifest(None)
        b = load_manifest()
        assert a is not b
        # Content identical though
        assert a == b

    def test_inject_fake_manifest(self):
        fake = {"hwmon": {"cpu_drivers": ["only_me"]}}
        reset_manifest(fake)
        m = load_manifest()
        assert m is fake
