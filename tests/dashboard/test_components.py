"""Static contract smoke for Lit components.

Real browser-render verification — следующая сессия с user'ом visually на
http://127.0.0.1:18889/. Здесь — что файлы валидны и компоненты регистрируются.
"""

from __future__ import annotations

from pathlib import Path

import pytest

COMPONENT_NAMES = (
    "live-telemetry-tile",
    "calibration-state-tile",
    "stack-rationale-tile",
    "adapters-health-tile",
    "discovered-signals-tile",
    "training-tile",
    "throttle-events-tile",
    "actuator-history-tile",
    "sparkline-tile",
    "neighbours-tile",
    "efficiency-tile",
    "drift-tile",
    "predictor-breakdown-tile",
)


@pytest.fixture
def components_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "coolstep" / "dashboard" / "static" / "components"


def test_base_module_exports_lit(components_dir: Path):
    text = (components_dir / "_base.js").read_text()
    assert "import {" in text
    assert "lit@3" in text
    assert "export {" in text


def test_each_component_imports_from_base(components_dir: Path):
    for name in COMPONENT_NAMES:
        text = (components_dir / f"{name}.js").read_text()
        assert "from './_base.js'" in text, name


def test_each_component_registers_custom_element(components_dir: Path):
    for name in COMPONENT_NAMES:
        text = (components_dir / f"{name}.js").read_text()
        assert f"customElements.define('{name}'" in text, name


def test_each_component_has_render_method(components_dir: Path):
    for name in COMPONENT_NAMES:
        text = (components_dir / f"{name}.js").read_text()
        assert "render() {" in text, name


def test_index_html_imports_all_components(components_dir: Path):
    html = (components_dir.parent / "index.html").read_text()
    for name in COMPONENT_NAMES:
        assert f"<{name}" in html, f"{name} missing from index.html"
        assert f"/static/components/{name}.js" in html, f"{name} script tag missing"


def test_shell_runtime_does_not_handle_migrated_tiles(components_dir: Path):
    """Tiles мигрировали в Lit components/. The new modular shell
    (instrument.js + pills/) only owns masthead pills — it must not
    touch tile-body DOM ids."""
    static_dir = components_dir.parent
    files: list[str] = [(static_dir / "instrument.js").read_text()]
    pills_dir = static_dir / "pills"
    if pills_dir.is_dir():
        for f in sorted(pills_dir.glob("*.js")):
            files.append(f.read_text())
    blob = "\n".join(files)
    forbidden_ids = (
        '"calibration-gates"',
        '"adapters-table"',
        '"adr-list"',
        '"kv-cpu-temp"',
        '"kv-model"',
        '"kv-prob"',
    )
    for forbidden in forbidden_ids:
        assert forbidden not in blob, f"shell still references {forbidden}"


def test_styles_uses_token_contract(components_dir: Path):
    """styles.css must declare the design token surface the audit demands."""
    css = (components_dir.parent / "styles.css").read_text()
    required_tokens = (
        "--surface-1", "--surface-2", "--surface-3",
        "--fg-muted", "--fg-dim",
        "--ok", "--ok-soft", "--warn", "--warn-soft", "--err", "--err-soft",
        "--font-mono", "--font-serif",
        "--sp-1", "--sp-2", "--sp-3", "--sp-4",
        "--header-h", "--blur-glass",
    )
    for tok in required_tokens:
        assert tok in css, f"styles.css missing token {tok}"


def test_styles_includes_skeleton_and_fade_keyframes(components_dir: Path):
    css = (components_dir.parent / "styles.css").read_text()
    assert "@keyframes skel-shimmer" in css
    assert "@keyframes card-fade-in" in css
    assert ":focus-visible" in css


def test_index_uses_pill_recipe(components_dir: Path):
    html = (components_dir.parent / "index.html").read_text()
    for pill_id in ("pill-mode", "pill-coverage", "pill-signals", "pill-health", "pill-stress"):
        assert pill_id in html, f"missing masthead pill: {pill_id}"


def test_instrument_js_wires_stress_pill(components_dir: Path):
    """The stress pill must be wired in the shell runtime. After the P2.4
    modularisation the wiring lives in pills/stress.js — instrument.js
    is the entry-point that imports it. We check both for completeness."""
    static_dir = components_dir.parent
    instrument = (static_dir / "instrument.js").read_text()
    assert "pills/stress" in instrument, "instrument.js must import pills/stress.js"
    stress_module = (static_dir / "pills" / "stress.js").read_text()
    assert "/api/stress-state" in stress_module
    assert "pill-stress" in stress_module
    assert "shell.pill.stress.idle" in stress_module


def test_legacy_dashboard_js_absent(components_dir: Path):
    """The pre-P2.4 monolithic dashboard.js must not coexist with the new
    instrument.js entry-point — that would leave the page fighting itself
    over the masthead pills."""
    assert not (components_dir.parent / "dashboard.js").exists(), \
        "legacy dashboard.js still present after modularisation"


def test_strings_carry_stress_pill_keys(components_dir: Path):
    text = (components_dir.parent / "i18n" / "strings.js").read_text()
    for key in ("shell.pill.stress.idle", "shell.pill.stress.running"):
        # Every supported lang carries the key (en/ru/uk × 2 keys = 6 occurrences).
        assert text.count(f"'{key}'") >= 3, f"{key} missing in some lang"


def test_strings_carry_actuator_tile_keys(components_dir: Path):
    text = (components_dir.parent / "i18n" / "strings.js").read_text()
    for key in (
        "tile.actuator.title",
        "tile.actuator.empty",
        "tile.actuator.col.ts",
        "tile.actuator.col.kind",
        "tile.actuator.mode.dry",
        "tile.actuator.ttl.live",
        "tile.actuator.ttl.expired",
        "tile.actuator.filter.all",
        "tile.actuator.filter.apply",
        "tile.actuator.filter.revert",
        "tile.actuator.filter.error",
        "tile.actuator.hero.entries",
        "tile.actuator.hero.armed",
    ):
        assert text.count(f"'{key}'") >= 3, f"{key} missing in some lang"


def test_strings_carry_predictor_breakdown_keys(components_dir: Path):
    text = (components_dir.parent / "i18n" / "strings.js").read_text()
    for key in (
        "tile.predictor.title",
        "tile.predictor.knn",
        "tile.predictor.trajectory",
        "tile.predictor.final",
        "tile.predictor.confidence",
        "tile.predictor.reason",
        "tile.predictor.expected_temp",
        "tile.predictor.model.cold",
        "tile.predictor.empty",
    ):
        assert text.count(f"'{key}'") >= 3, f"{key} missing in some lang"
