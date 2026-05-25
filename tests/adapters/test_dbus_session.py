"""Tests for dbus_session collector — gdbus subprocess mocked."""

from __future__ import annotations

from coolstep.adapters.collectors.dbus_session import (
    DbusSessionCollector,
    _query_idle_time_ms,
    _query_kde_active,
)


class TestParsing:
    def test_kde_active_window_parse(self, monkeypatch):
        # Simulate two gdbus calls: activeWindow → uint32 4842, then resourceClass
        outputs = [
            "(uint32 4842,)",
            "(<'Firefox'>,)",
        ]
        idx = [0]

        def fake_call(*a, **kw):
            out = outputs[idx[0]]
            idx[0] += 1
            return out

        monkeypatch.setattr(
            "coolstep.adapters.collectors.dbus_session._gdbus_call", fake_call,
        )
        result = _query_kde_active()
        assert result == "Firefox"

    def test_idle_time_parse(self, monkeypatch):
        monkeypatch.setattr(
            "coolstep.adapters.collectors.dbus_session._gdbus_call",
            lambda *a, **kw: "(uint32 12345,)",
        )
        idle = _query_idle_time_ms()
        assert idle == 12345

    def test_idle_time_failed_call(self, monkeypatch):
        monkeypatch.setattr(
            "coolstep.adapters.collectors.dbus_session._gdbus_call",
            lambda *a, **kw: None,
        )
        idle = _query_idle_time_ms()
        assert idle is None


class TestDiscover:
    def test_no_dbus_addr(self, monkeypatch):
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        c = DbusSessionCollector()
        assert c.discover() is False

    def test_no_gdbus(self, monkeypatch):
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:/tmp/dbus")
        monkeypatch.setattr(
            "coolstep.adapters.collectors.dbus_session.shutil.which",
            lambda _: None,
        )
        c = DbusSessionCollector()
        assert c.discover() is False

    def test_skip_on_hyprland(self, monkeypatch):
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:/tmp/dbus")
        monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abc")
        monkeypatch.setattr(
            "coolstep.adapters.collectors.dbus_session.shutil.which",
            lambda _: "/usr/bin/gdbus",
        )
        c = DbusSessionCollector()
        assert c.discover() is False


class TestSample:
    def test_render_workload_label(self, monkeypatch):
        c = DbusSessionCollector()
        partial = c._render("vscode", 1500)
        assert partial["workload"].label == "vscode"
        assert partial["platform_state"]["dbus.idle_ms"] == "1500"

    def test_render_empty_no_label(self):
        c = DbusSessionCollector()
        partial = c._render(None, None)
        assert partial == {}

    def test_signals_have_focused_app(self):
        c = DbusSessionCollector()
        sig_names = {s.name for s in c.signals()}
        assert "workload.focused_app" in sig_names
        assert "workload.idle_ms" in sig_names

    def test_sample_uses_cache(self, monkeypatch):
        c = DbusSessionCollector()
        c._cached_label = "kitty"
        c._cached_idle_ms = 500
        # Set cached_at to now so cache window applies
        import time
        c._cached_at = time.monotonic()
        partial = c.sample()
        # Cached value should be returned without subprocess
        assert partial["workload"].label == "kitty"
