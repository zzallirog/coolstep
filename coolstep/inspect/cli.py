"""coolstep CLI — adapters / tail / stats / export."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import click

from coolstep.adapters.collectors import discover as discover_collectors
from coolstep.core.schema import ActionVerb, TelemetryFrame, merge_partial


def _coolstep_home() -> Path:
    return Path(os.environ.get("COOLSTEP_HOME", str(Path.home() / "coolstep" / "data")))


def _l2_default_path() -> Path:
    """Default Layer-2 custom manifest path (XDG-respecting)."""
    raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(raw) if raw else (Path.home() / ".config")
    return base / "coolstep" / "custom.json"


def _store_path() -> Path:
    return _coolstep_home() / "store.db"


@click.group()
def main() -> None:
    """coolstep — predictive soft-cooling."""


@main.command()
@click.option("--detailed/--brief", default=False, help="Show per-actuator runtime detail")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of human-readable output")
def adapters(detailed: bool, as_json: bool) -> None:
    """List discovered collectors + actuators (optionally with runtime detail)."""
    from coolstep.adapters.actuators import discover as discover_actuators

    collectors = discover_collectors()
    actuators = discover_actuators()

    if as_json:
        body: dict = {
            "collectors": [{"name": c.name} for c in collectors],
            "actuators": [],
        }
        if detailed:
            from coolstep.inspect.adapters_detail import detail_all
            body["actuators"] = detail_all(actuators)
        else:
            body["actuators"] = [
                {"name": a.name, "supports": [v.value for v in ActionVerb if a.supports(v)]}
                for a in actuators
            ]
        click.echo(json.dumps(body, indent=2, default=str))
        return

    # human-readable
    click.echo(f"collectors ({len(collectors)}):")
    if collectors:
        width = max(len(c.name) for c in collectors)
        for c in collectors:
            cost = c.cost()
            click.echo(
                f"  {c.name:<{width}}  status=ok  sample_us={cost.sample_us}  rss_kb={cost.rss_kb}"
            )
    else:
        click.echo("  (none)")

    click.echo(f"actuators ({len(actuators)}):")
    if detailed:
        from coolstep.inspect.adapters_detail import detail_for
        for a in actuators:
            d = detail_for(a)
            click.echo(f"  - {d['name']}")
            click.echo(f"      supports:        {', '.join(d['supports']) or '—'}")
            click.echo(f"      armed:           {d['armed']['armed']}")
            if d["armed"]["armed"]:
                click.echo(f"      armed verb:      {d['armed']['verb']}")
                click.echo(f"      ttl remaining:   {d['armed']['ttl_remaining_sec']:.0f}s")
            if d["baseline"]:
                if d["baseline"].get("baseline_present"):
                    click.echo(
                        f"      baseline:        {d['baseline']['baseline_anchor_count']} anchors, "
                        f"{d['baseline']['baseline_age_sec']:.0f}s old"
                    )
                else:
                    click.echo("      baseline:        (none)")
            click.echo(f"      journal entries: {d['journal_count_last_1000']} (last 1000)")
    else:
        for a in actuators:
            verbs = [v.value for v in ActionVerb if a.supports(v)]
            click.echo(f"  - {a.name}  [{', '.join(verbs) or 'no verbs'}]")
    if not actuators:
        click.echo("  (none)")


@main.command()
@click.option("--ticks", default=10, type=int, help="Number of ticks to print")
@click.option("--period", default=1.0, type=float)
def tail(ticks: int, period: float) -> None:
    """Live print N consecutive ticks gathered from collectors directly."""
    collectors = discover_collectors()
    if not collectors:
        click.echo("No collectors available.", err=True)
        sys.exit(1)
    click.echo(f"# Tail {ticks} ticks @ {period}s, collectors: {[c.name for c in collectors]}")
    for i in range(ticks):
        frame = TelemetryFrame(timestamp=time.time())
        for c in collectors:
            try:
                merge_partial(frame, c.sample())
            except Exception as exc:  # noqa: BLE001
                click.echo(f"# {c.name} failed: {exc}", err=True)
        click.echo(_format_frame_oneline(i, frame))
        if i < ticks - 1:
            time.sleep(period)


@main.command()
@click.option("--since", default="24h", help="Window: 5m / 1h / 24h / 7d")
def stats(since: str) -> None:
    """Print summary stats from the persistent store."""
    path = _store_path()
    if not path.exists():
        click.echo(f"No store at {path} — daemon not running yet?", err=True)
        sys.exit(1)
    seconds = _parse_window(since)
    cutoff = time.time() - seconds
    conn = sqlite3.connect(path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        recent = conn.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts), MIN(cpu_temp), MAX(cpu_temp), AVG(cpu_temp) "
            "FROM frames WHERE ts >= ?",
            (cutoff,),
        ).fetchone()
        throttle_count = conn.execute(
            "SELECT COUNT(*) FROM throttle_events WHERE ts_start >= ?", (cutoff,)
        ).fetchone()[0]
    finally:
        conn.close()
    cnt, ts_min, ts_max, t_min, t_max, t_avg = recent
    if cnt == 0:
        click.echo(f"# No frames in last {since}.")
        click.echo(f"# Total frames in store: {total}")
        return
    span = (ts_max - ts_min) if ts_max and ts_min else 0
    click.echo(f"window           : last {since} ({seconds:.0f}s)")
    click.echo(f"frames in window : {cnt}")
    click.echo(f"frames total     : {total}")
    click.echo(f"span             : {span:.1f}s ({span / 3600:.2f}h)")
    if t_min is not None and t_max is not None and t_avg is not None:
        click.echo(f"cpu_temp min/max : {t_min:.1f} / {t_max:.1f} °C")
        click.echo(f"cpu_temp avg     : {t_avg:.1f} °C")
    click.echo(f"throttle events  : {throttle_count}")


@main.command()
def drift() -> None:
    """Print model drift indicators based on ml-state.json + history."""
    from coolstep.core.drift import evaluate

    state = _coolstep_home() / "ml-state.json"
    history = _coolstep_home() / "drift-history.jsonl"
    report = evaluate(state, history)
    click.echo(f"severity         : {report.severity:.2f}")
    click.echo(f"snapshot age     : {report.snapshot_age_sec or '—'} s")
    click.echo(f"history samples  : {report.samples_for_baseline}")
    if not report.indicators:
        click.echo("no drift indicators triggered")
        return
    click.echo("indicators:")
    for ind in report.indicators:
        click.echo(f"  - {ind.name:25} sev={ind.severity:.2f}  {ind.note}")


@main.command()
@click.option("--since", default="7d")
def efficiency(since: str) -> None:
    """Print historical efficiency curve (work-per-degree by temp bucket)."""
    from coolstep.core.efficiency import compute_historical

    seconds = _parse_window(since)
    report = compute_historical(_store_path(), since_seconds=seconds)
    click.echo(
        f"# T_ambient={report.t_ambient}°C  samples={report.sample_count}  bins={len(report.bins)}"
    )
    if report.sweet_spot_temp is not None:
        click.echo(
            f"# sweet_spot @ {report.sweet_spot_temp:.1f}°C "
            f"(eff={report.sweet_spot_efficiency:.3f})"
        )
    if report.knee_temp is not None:
        click.echo(f"# knee @ {report.knee_temp:.1f}°C (efficiency drops to <70% of peak)")
    click.echo(f"\n{'temp range':<14}{'samples':>9}{'mean':>10}{'p50':>10}{'p95':>10}")
    for b in report.bins:
        click.echo(
            f"{b.temp_low:>5.1f} – {b.temp_high:<5.1f}{b.samples:>9}"
            f"{b.mean_efficiency:>10.3f}{b.p50_efficiency:>10.3f}{b.p95_efficiency:>10.3f}"
        )


@main.command()
@click.option("--since", default="24h")
@click.option("--format", "fmt", type=click.Choice(["csv", "json"]), default="csv")
@click.option("--out", default="-", help='Output path or "-" for stdout')
def export(since: str, fmt: str, out: str) -> None:
    """Export frames in window."""
    path = _store_path()
    if not path.exists():
        click.echo(f"No store at {path}.", err=True)
        sys.exit(1)
    cutoff = time.time() - _parse_window(since)
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT ts, cpu_temp, cpu_power, gpu_temp, gpu_power, fan_max_rpm, "
            "workload_label FROM frames WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    cols = ["ts", "cpu_temp", "cpu_power", "gpu_temp", "gpu_power", "fan_max_rpm", "workload_label"]
    sink = sys.stdout if out == "-" else open(out, "w", encoding="utf-8")
    try:
        if fmt == "csv":
            writer = csv.writer(sink)
            writer.writerow(cols)
            writer.writerows(rows)
        else:
            sink.write(json.dumps([dict(zip(cols, r, strict=True)) for r in rows], indent=2))
    finally:
        if sink is not sys.stdout:
            sink.close()


@main.command()
@click.option("--json", "as_json", is_flag=True, help="emit JSON instead of table")
def doctor(as_json: bool) -> None:
    """Run health checks. Exit code: 0=ok, 1=warn, 2=fail."""
    from coolstep.inspect.doctor import format_table, run_all

    results, code = run_all()
    if as_json:
        click.echo(json.dumps([
            {"name": r.name, "status": r.status, "message": r.message}
            for r in results
        ], indent=2))
    else:
        click.echo(format_table(results))
        click.echo("")
        verdict = {0: "✓ healthy", 1: "⚠ degraded (some warnings)", 2: "✗ critical"}[code]
        click.echo(f"verdict: {verdict}")
    sys.exit(code)


@main.command("export-telemetry")
@click.option("--since", default="1h", help="Time window (s/m/h/d suffix)")
@click.option(
    "--format", "fmt", type=click.Choice(["csv", "jsonl"]), default="csv"
)
@click.option("--out", "-o", default="-", help="Output path or `-` for stdout")
def export_telemetry(since: str, fmt: str, out: str) -> None:
    """Dump telemetry frames from store.db over a time range."""
    from coolstep.inspect.telemetry_export import export_telemetry as _exp

    store_p = _store_path()
    if not store_p.exists():
        click.echo(f"No store at {store_p}.", err=True)
        sys.exit(1)
    if out == "-":
        n = _exp(store_p, since, fmt, sys.stdout)
    else:
        with open(out, "w", encoding="utf-8", newline="") as f:
            n = _exp(store_p, since, fmt, f)
    click.echo(f"wrote {n} rows", err=True)


@main.command()
@click.option("--since", default="24h", help="Time window (s/m/h/d suffix)")
@click.option("--json", "as_json", is_flag=True, help="emit JSON instead of table")
def history(since: str, as_json: bool) -> None:
    """Summarise decisions.jsonl over a time range."""
    from coolstep.inspect.history import format_summary, summarise
    from coolstep.inspect.telemetry_export import _parse_window as _pw

    p = _coolstep_home() / "decisions.jsonl"
    s = summarise(p, _pw(since))
    if as_json:
        click.echo(json.dumps(s, indent=2, default=str))
    else:
        click.echo(format_summary(s))


@main.command("export-profile")
@click.option("--out", "-o", default="-", help="Output path or `-` for stdout")
def export_profile(out: str) -> None:
    """Export this host's tuned coolstep profile as JSON."""
    from coolstep.inspect.profile_io import export_profile as _export

    home = _coolstep_home()
    profile = _export(home)
    text = json.dumps(profile, indent=2)
    if out == "-":
        click.echo(text)
    else:
        Path(out).write_text(text)
        click.echo(f"wrote {out}")


@main.command("import-profile")
@click.argument("path")
@click.option("--dry-run", is_flag=True)
def import_profile(path: str, dry_run: bool) -> None:
    """Apply a previously-exported profile to this install."""
    from coolstep.inspect.profile_io import import_profile as _import

    profile = json.loads(Path(path).read_text())
    home = _coolstep_home()
    result = _import(profile, home, dry_run=dry_run)
    click.echo(f"version: {result['version']}")
    click.echo(f"baseline_written: {result['baseline_written']}")
    if result["drop_in_suggested"]:
        click.echo(
            "\nSuggested drop-in"
            " (paste into ~/.config/systemd/user/coolstep-collector.service.d/90-imported.conf):"
        )
        click.echo(result["drop_in_suggested"])
    if result["threshold_diff"]:
        click.echo("\nThreshold differences (imported -> local):")
        for k, (theirs, ours) in result["threshold_diff"].items():
            click.echo(f"  {k}: {theirs} -> {ours}")


def _parse_window(s: str) -> float:
    s = s.strip().lower()
    if s.endswith("s"):
        return float(s[:-1])
    if s.endswith("m"):
        return float(s[:-1]) * 60
    if s.endswith("h"):
        return float(s[:-1]) * 3600
    if s.endswith("d"):
        return float(s[:-1]) * 86400
    return float(s)


def _format_frame_oneline(idx: int, frame: TelemetryFrame) -> str:
    tctl = frame.cpu.temps_c.get("tctl") or frame.cpu.temps_c.get("tdie") or 0.0
    freq = frame.cpu.freq_mhz[0] if frame.cpu.freq_mhz else 0.0
    fan = max((f.rpm for f in frame.fans if f.rpm is not None), default=0)
    gpu_temps = [g.temp_c for g in frame.gpus if g.temp_c is not None]
    gpu = max(gpu_temps) if gpu_temps else 0.0
    apps = frame.workload.rolling_features.get("visible_apps", 0) if frame.workload else 0
    profile = frame.platform_state.get("platform_profile", "?")
    epp = frame.platform_state.get("epp", "?")
    return (
        f"#{idx:03d} cpu_tctl={tctl:5.1f}°C cpu_f0={freq:6.0f}MHz "
        f"gpu_max={gpu:5.1f}°C fan_max={fan:5d}RPM apps={int(apps):2d} "
        f"profile={profile:11s} epp={epp}"
    )


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of human-readable output")
@click.option("--install-plan", "show_install", is_flag=True, help="Show package install recommendations")
@click.option(
    "--facet",
    type=click.Choice(["desktop", "server", "ambiguous"], case_sensitive=False),
    default=None,
    help="Force deployment-target preset: desktop = laptop/NUC tuning, server = rack/BMC tuning",
)
@click.option(
    "--manifest-paths", "show_manifest", is_flag=True,
    help="Show three-layer manifest paths (L0=core, L1=community, L2=custom)",
)
def compat(as_json: bool, show_install: bool, facet: str | None, show_manifest: bool) -> None:
    """Show platform compatibility report and install plan.

    coolstep has two deployment targets:
      - desktop  → quiet & responsive laptop/workstation
      - server   → foresight panel for rack/homelab uptime

    `--facet` forces a packaging preset; absent, the caps.facet auto-detect
    runs (battery / DE / ipmi kmod → desktop vs server).
    """
    from coolstep.compat import get_caps, manifest_paths
    from coolstep.compat.install_plan import caps_to_install_plan

    if show_manifest:
        paths = manifest_paths()
        click.echo("=== coolstep manifest layers ===")
        click.echo("")
        for layer, name in [("L0", "core (bundled)"), ("L1", "community"), ("L2", "custom (user)")]:
            key = layer.lower()
            path = paths.get(key)
            mark = "✓" if path else "—"
            click.echo(f"  {mark} {layer} {name:20s} {path or '(absent)'}")
        click.echo("")
        click.echo("Drop a custom override at: " + str(_l2_default_path()))
        return

    caps = get_caps()
    effective_facet = facet or caps.facet

    if as_json:
        import dataclasses
        body = dataclasses.asdict(caps)
        body["facet"] = effective_facet
        click.echo(json.dumps(body, indent=2, default=str))
        return

    if show_install:
        plan = caps_to_install_plan(caps, facet_override=facet)
        click.echo(plan.summary())
        return

    # Default: human-readable capability report
    lines = [
        "=== coolstep platform report ===",
        "",
        f"Target:    {effective_facet}"
                  + ("  (auto-detected)" if not facet else "  (forced)"),
        f"Distro:    {caps.distro_name} ({caps.distro_id})",
        f"Clan:      {caps.distro_clan}  pkg-manager: {caps.pkg_manager or 'unknown'}",
        f"Host type: {'virtual' if caps.is_virtual else 'physical'}"
                  f"  arch={caps.cpu_arch}"
                  f"  thermal_zones={caps.thermal_zone_count}",
        f"CPU:       {caps.cpu_vendor.upper()}  {caps.cpu_model}",
        f"  hwmon:   {'k10temp ' if caps.hwmon_k10temp else ''}"
                  f"{'coretemp ' if caps.hwmon_coretemp else ''}"
                  f"{'zenpower ' if caps.hwmon_zenpower else ''}"
                  f"{'amd_energy ' if caps.hwmon_amd_energy else ''}"
                  f"{'RAPL ' if caps.hwmon_rapl else ''}".strip() or "none found",
        f"  super-I/O: {' '.join(caps.hwmon_superio) if caps.hwmon_superio else 'none'}",
        f"  RAPL:    {'readable' if caps.rapl_readable else 'present but locked' if caps.rapl_powercap else 'absent'}",
        f"  perf:    paranoid={caps.perf_event_paranoid}"
                  f" binary={'yes' if caps.perf_binary_available else 'no'}",
        f"  EPP:     {'readable' if caps.epp_available else 'unavailable'}"
                  f"{' writable' if caps.epp_writable else ''}",
        f"  profile: {'yes' if caps.platform_profile_available else 'no'}",
        f"GPU:       primary={caps.gpu_primary}"
                  f"  amd={caps.gpu_amd} nvidia={caps.gpu_nvidia} intel={caps.gpu_intel}",
        f"  pynvml:  {'importable' if caps.pynvml_importable else 'not importable'}",
        f"WM:        {caps.compositor}",
        f"  hyprctl: {caps.hyprctl_available}  swaymsg: {caps.swaymsg_available}",
        "",
        "Fan tools:",
        f"  asusctl: {caps.asusctl_available}"
                  f"{' v' + caps.asusctl_version if caps.asusctl_version else ''}"
                  f"{' ✓' if caps.asusctl_version_ok else ' (upgrade required)' if caps.asusctl_available else ''}",
        f"  nbfc:    {caps.nbfc_available}   fancontrol: {caps.fancontrol_available}",
        f"  thinkfan:{caps.thinkfan_available}  coolercontrol: {caps.coolercontrol_available}",
        f"  dell-smm:{caps.dell_smm_hwmon}",
        "",
        "Power tools:",
        f"  ryzenadj:  {caps.ryzenadj_available}",
        f"  ppd active:{caps.power_profiles_daemon_active}  tlp active: {caps.tlp_active}",
        f"  auto-cpufreq:{caps.auto_cpufreq_available}",
        "",
        "Collectors expected:",
    ]
    for name in caps.collectors_expected:
        lines.append(f"  ✓ {name}")
    lines.append("")
    lines.append("Actuators expected:")
    lines.append("  ✓ readonly_log")
    for name in caps.actuators_expected:
        lines.append(f"  ✓ {name}")

    if caps.warnings:
        lines += ["", "Warnings:"]
        for w in caps.warnings:
            lines.append(f"  ⚠ {w}")

    lines += [
        "",
        "Enterprise / server:",
        f"  ipmitool:   {caps.ipmitool_available}",
        f"  redfish:    {'configured' if caps.redfish_endpoint_configured else 'no COOLSTEP_REDFISH_URL'}",
        f"  bpftrace:   {caps.bpftrace_available}   bcc: {caps.bcc_importable}",
        f"  ipmi kmod:  {caps.kmod_ipmi}",
        "",
        f"Secure boot: {caps.secure_boot}",
        f"systemd user: {caps.systemd_user_available}",
        "",
        "Run with --install-plan for package recommendations.",
    ]

    click.echo("\n".join(lines))


@main.command("install-units")
@click.option("--force", is_flag=True, help="Overwrite existing unit files in ~/.config/systemd/user/")
def install_units(force: bool) -> None:
    """Write coolstep systemd user units to ~/.config/systemd/user/.

    For pipx / pip --user installs, the systemd unit files are not
    dropped on disk automatically.  Run this once after install; then
    `systemctl --user enable --now coolstep-collector coolstep-dashboard`.

    AUR builds install the units to /usr/lib/systemd/user/ already —
    this command is for non-AUR install paths.
    """
    from coolstep.inspect.install_units import install as _install
    results = _install(force=force)
    for name, status in results:
        click.echo(f"  {name}: {status}")
    click.echo("")

    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    has_user_session = bool(xdg_runtime) and Path(xdg_runtime).exists()

    if has_user_session:
        click.echo("Next steps:")
        click.echo("  systemctl --user daemon-reload")
        click.echo("  systemctl --user enable --now coolstep-collector coolstep-dashboard")
        click.echo("  xdg-open http://127.0.0.1:18889/")
    else:
        default_runtime = f"/run/user/{os.getuid()}"
        click.echo("⚠ No systemd user session detected (XDG_RUNTIME_DIR unset or missing).")
        click.echo("  Typical for headless servers reached via SSH.  `systemctl --user`")
        click.echo("  would fail with 'Failed to connect to user scope bus' until you fix it.")
        click.echo("")
        click.echo("One-time setup (then re-login OR export var manually):")
        click.echo("  sudo loginctl enable-linger $USER")
        click.echo(f"  export XDG_RUNTIME_DIR={default_runtime}    # for current shell")
        click.echo("")
        click.echo("Then:")
        click.echo("  systemctl --user daemon-reload")
        click.echo("  systemctl --user enable --now coolstep-collector coolstep-dashboard")
        click.echo("  curl http://127.0.0.1:18889/api/health    # no xdg-open on headless")


if __name__ == "__main__":  # pragma: no cover
    main()
