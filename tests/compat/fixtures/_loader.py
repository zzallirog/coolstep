"""Snapshot loader + fake_platform ctx-manager.

Snapshot DSL (per-platform `snapshot.py`):

    SNAPSHOT: dict = {
        "name":            "asus-tuf-a15-7940hs",
        "description":     "...",
        "os_release":      "ID=arch\\nNAME=\\"Arch Linux\\"\\n",
        "cpuinfo":         "vendor_id\\t: AuthenticAMD\\n...",
        "uname_machine":   "x86_64",
        "dmi":             {"sys_vendor": "...", "product_name": "...", "chassis_vendor": "..."},
        "hwmon":           [{"name": "k10temp"}, {"name": "amdgpu"}, ...],
        "drm_cards":       [{"vendor": "0x1002"}, {"vendor": "0x10de"}],
        "modules":         ["amdgpu", "nvidia"],
        "thermal_zones":   8,
        "powercap_rapl":   True,
        "rapl_readable":   True,
        "epp":             {"present": True, "value": "balance_performance", "writable": True},
        "platform_profile": "balanced",
        "perf_event_paranoid": 2,
        "secure_boot":     False,
        "efi_vars":        True,
        "power_supply":    ["BAT0", "AC0"],
        "env":             {"HYPRLAND_INSTANCE_SIGNATURE": "..."},
        "which":           {"asusctl": True, "hyprctl": True, ...},
        "asusctl_version": "6.1.2",
        "py_modules":      {"pynvml": True, "bcc": False},
        "subprocess":      {("systemctl", "is-active", "--quiet", "tlp.service"): 3},
        "expected":        {"cpu_vendor": "amd", "facet": "desktop", ...},
    }
"""
from __future__ import annotations

import importlib.util
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

FIXTURES_DIR = Path(__file__).parent


@dataclass
class Snapshot:
    name: str
    description: str = ""
    os_release: str = ""
    cpuinfo: str = ""
    uname_machine: str = "x86_64"
    dmi: dict[str, str] = field(default_factory=dict)
    hwmon: list[dict[str, Any]] = field(default_factory=list)
    drm_cards: list[dict[str, str]] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    thermal_zones: int = 0
    powercap_rapl: bool = False
    rapl_readable: bool = False
    epp: dict[str, Any] = field(default_factory=dict)
    platform_profile: str | None = None
    perf_event_paranoid: int | None = 2
    secure_boot: bool | None = None
    efi_vars: bool = False
    power_supply: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    which: dict[str, bool] = field(default_factory=dict)
    asusctl_version: str = ""
    py_modules: dict[str, bool] = field(default_factory=dict)
    subprocess: dict[tuple[str, ...], int] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)
    # /run/user/<uid>/hypr/<sig>/.socket.sock — emulate Hyprland socket
    hypr_socket: str | None = None  # signature; presence => socket exists

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        keys = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in keys})


def list_snapshots() -> list[str]:
    """All snapshot names found in fixtures/."""
    out: list[str] = []
    for p in FIXTURES_DIR.iterdir():
        if p.is_dir() and (p / "snapshot.py").is_file():
            out.append(p.name)
    return sorted(out)


def load_snapshot(name: str) -> Snapshot:
    """Load snapshot DSL from fixtures/<name>/snapshot.py — module must export SNAPSHOT."""
    path = FIXTURES_DIR / name / "snapshot.py"
    if not path.is_file():
        raise FileNotFoundError(f"snapshot not found: {path}")
    spec = importlib.util.spec_from_file_location(f"snapshot.{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    raw = mod.SNAPSHOT  # type: ignore[attr-defined]
    if "name" not in raw:
        raw = {"name": name, **raw}
    return Snapshot.from_dict(raw)


def apply_overlay(base: Snapshot, overlay: dict[str, Any]) -> Snapshot:
    """Return a new Snapshot with overlay fields replacing/extending base.

    For dict-typed fields (env, which, py_modules, epp, dmi, expected) we
    shallow-merge; for lists (hwmon, drm_cards, modules, power_supply) we
    replace if overlay provides a value. Scalars replace.
    """
    merged: dict[str, Any] = {}
    for f in Snapshot.__dataclass_fields__.values():  # type: ignore[attr-defined]
        cur = getattr(base, f.name)
        if f.name not in overlay:
            merged[f.name] = cur
            continue
        new = overlay[f.name]
        if isinstance(cur, dict) and isinstance(new, dict):
            merged[f.name] = {**cur, **new}
        else:
            merged[f.name] = new
    return Snapshot(**merged)


def _materialise(snap: Snapshot, root: Path) -> None:
    """Write the fake /sys, /proc, /etc tree under `root` per snapshot DSL."""
    sysdir = root / "sys"
    proc = root / "proc"
    etc = root / "etc"
    for d in (sysdir, proc, etc):
        d.mkdir(parents=True, exist_ok=True)

    # /etc/os-release
    if snap.os_release:
        (etc / "os-release").write_text(snap.os_release, encoding="utf-8")

    # /proc/cpuinfo
    if snap.cpuinfo:
        (proc / "cpuinfo").write_text(snap.cpuinfo, encoding="utf-8")

    # /proc/sys/kernel/perf_event_paranoid
    if snap.perf_event_paranoid is not None:
        ppath = proc / "sys" / "kernel"
        ppath.mkdir(parents=True, exist_ok=True)
        (ppath / "perf_event_paranoid").write_text(
            f"{snap.perf_event_paranoid}\n", encoding="utf-8",
        )

    # /sys/class/dmi/id/
    if snap.dmi:
        dmi = sysdir / "class" / "dmi" / "id"
        dmi.mkdir(parents=True, exist_ok=True)
        for key, val in snap.dmi.items():
            (dmi / key).write_text(val + "\n", encoding="utf-8")

    # /sys/class/hwmon/hwmon<i>/name
    if snap.hwmon:
        hwmon_root = sysdir / "class" / "hwmon"
        hwmon_root.mkdir(parents=True, exist_ok=True)
        for i, dev in enumerate(snap.hwmon):
            hd = hwmon_root / f"hwmon{i}"
            hd.mkdir(parents=True, exist_ok=True)
            for k, v in dev.items():
                (hd / k).write_text(f"{v}\n", encoding="utf-8")

    # /sys/class/drm/cardN/device/vendor
    if snap.drm_cards:
        drm = sysdir / "class" / "drm"
        drm.mkdir(parents=True, exist_ok=True)
        for i, card in enumerate(snap.drm_cards):
            dev = drm / f"card{i}" / "device"
            dev.mkdir(parents=True, exist_ok=True)
            for k, v in card.items():
                (dev / k).write_text(f"{v}\n", encoding="utf-8")

    # /sys/module/<mod>/
    if snap.modules:
        mods = sysdir / "module"
        mods.mkdir(parents=True, exist_ok=True)
        for m in snap.modules:
            (mods / m).mkdir(parents=True, exist_ok=True)

    # /sys/class/thermal/thermal_zone<N>
    if snap.thermal_zones:
        tz = sysdir / "class" / "thermal"
        tz.mkdir(parents=True, exist_ok=True)
        for i in range(snap.thermal_zones):
            (tz / f"thermal_zone{i}").mkdir(parents=True, exist_ok=True)

    # /sys/devices/virtual/powercap/intel-rapl/intel-rapl:0/energy_uj
    if snap.powercap_rapl:
        rapl = sysdir / "devices" / "virtual" / "powercap" / "intel-rapl"
        pkg = rapl / "intel-rapl:0"
        pkg.mkdir(parents=True, exist_ok=True)
        if snap.rapl_readable:
            (pkg / "energy_uj").write_text("42\n", encoding="utf-8")
        else:
            # Create file but make read fail by chmod 000
            ej = pkg / "energy_uj"
            ej.write_text("42\n", encoding="utf-8")
            ej.chmod(0o000)

    # /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference
    if snap.epp.get("present"):
        epp_dir = sysdir / "devices" / "system" / "cpu" / "cpu0" / "cpufreq"
        epp_dir.mkdir(parents=True, exist_ok=True)
        ef = epp_dir / "energy_performance_preference"
        ef.write_text(snap.epp.get("value", "balance_performance") + "\n", encoding="utf-8")
        if not snap.epp.get("writable", True):
            ef.chmod(0o444)

    # /sys/firmware/acpi/platform_profile
    if snap.platform_profile is not None:
        pf = sysdir / "firmware" / "acpi"
        pf.mkdir(parents=True, exist_ok=True)
        (pf / "platform_profile").write_text(snap.platform_profile + "\n", encoding="utf-8")

    # /sys/firmware/efi/efivars/SecureBoot-...
    if snap.efi_vars:
        ev = sysdir / "firmware" / "efi" / "efivars"
        ev.mkdir(parents=True, exist_ok=True)
        if snap.secure_boot is not None:
            # 4-byte attrs + 1-byte value
            val = bytes([6, 0, 0, 0]) + bytes([1 if snap.secure_boot else 0])
            (ev / "SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c").write_bytes(val)

    # /sys/class/power_supply/BATN
    if snap.power_supply:
        ps = sysdir / "class" / "power_supply"
        ps.mkdir(parents=True, exist_ok=True)
        for name in snap.power_supply:
            (ps / name).mkdir(parents=True, exist_ok=True)

    # Hyprland socket — under XDG_RUNTIME_DIR if env says so
    if snap.hypr_socket:
        xdg = snap.env.get("XDG_RUNTIME_DIR")
        if xdg:
            hd = root / xdg.lstrip("/") / "hypr" / snap.hypr_socket
            hd.mkdir(parents=True, exist_ok=True)
            (hd / ".socket.sock").touch()


def _make_fake_read(snap: Snapshot, root: Path):
    """Patched _read: redirect bare /proc, /sys, /etc paths into root.

    Paths that already live under `root` (because the caller already went
    through _redirecting_path) are read as-is. This avoids double-redirect.
    """
    root_str = str(root)
    redirect_prefixes = ("/proc", "/sys", "/etc")
    def fake_read(path):
        try:
            p = Path(path)
        except TypeError:
            return None
        s = str(p)
        if s.startswith(root_str):
            mapped = p
        elif s.startswith(redirect_prefixes):
            rel = s.lstrip("/")
            mapped = root / rel
        else:
            mapped = p
        try:
            raw = mapped.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return raw.strip()
    return fake_read


def _make_fake_which(snap: Snapshot):
    table = dict(snap.which)
    def which(name: str):
        present = table.get(name, False)
        return f"/usr/bin/{name}" if present else None
    return which


def _make_fake_subprocess_run(snap: Snapshot):
    sub_table: dict[tuple[str, ...], int] = dict(snap.subprocess)
    version = snap.asusctl_version

    class FakeCompleted:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def run(cmd, *args, **kwargs):
        key = tuple(cmd) if isinstance(cmd, (list, tuple)) else (cmd,)
        # asusctl --version family
        if key[:1] == ("asusctl",) and "--version" in key:
            if version:
                return FakeCompleted(stdout=f"asusctl {version}\n", returncode=0)
            return FakeCompleted(stderr="unknown flag\n", returncode=2)
        if key[:2] == ("pacman", "-Q") and "asusctl" in key:
            return (
                FakeCompleted(stdout=f"asusctl {version}-1\n", returncode=0)
                if version
                else FakeCompleted(returncode=1)
            )
        # systemctl is-active
        if key[:2] == ("systemctl", "is-active"):
            unit = key[-1]
            rc = sub_table.get(("systemctl", "is-active", unit), 3)
            return FakeCompleted(returncode=rc)
        # Generic table lookup
        if key in sub_table:
            return FakeCompleted(returncode=sub_table[key])
        return FakeCompleted(returncode=127)

    return run


def _make_fake_find_spec(snap: Snapshot):
    table = dict(snap.py_modules)
    real = importlib.util.find_spec

    def find_spec(name, *args, **kwargs):
        if name in table:
            return object() if table[name] else None
        return real(name, *args, **kwargs)

    return find_spec


def _make_fake_service_active(snap: Snapshot):
    """Compatible with `_service_active(name)` that runs `systemctl is-active`."""
    sub_table = dict(snap.subprocess)

    def active(name: str) -> bool:
        return sub_table.get(("systemctl", "is-active", name), 3) == 0

    return active


@contextmanager
def fake_platform(snap: Snapshot, tmp_root: Path) -> Iterator[Path]:
    """Materialise snapshot under tmp_root and patch detect.py to read it.

    Usage:
        with fake_platform(snap, tmp_path) as root:
            caps = detect_caps()
            assert caps.cpu_vendor == snap.expected["cpu_vendor"]
    """
    _materialise(snap, tmp_root)
    sysdir = tmp_root / "sys"

    fake_read = _make_fake_read(snap, tmp_root)
    fake_which = _make_fake_which(snap)
    fake_run = _make_fake_subprocess_run(snap)
    fake_find_spec = _make_fake_find_spec(snap)
    fake_active = _make_fake_service_active(snap)

    # Env: clear & set per snapshot
    env_overlay = dict(snap.env)
    # Make Path.home() point at our fake to keep systemd_unit_user_dir local
    env_overlay.setdefault("HOME", str(tmp_root / "home" / "user"))
    (Path(env_overlay["HOME"])).mkdir(parents=True, exist_ok=True)

    patches = [
        patch("coolstep.compat.detect._HWMON_ROOT", sysdir / "class" / "hwmon"),
        patch("coolstep.compat.detect._CPU_ROOT", sysdir / "devices" / "system" / "cpu"),
        patch("coolstep.compat.detect._DRM_ROOT", sysdir / "class" / "drm"),
        patch("coolstep.compat.detect._MODULE_ROOT", sysdir / "module"),
        patch(
            "coolstep.compat.detect._RAPL_ROOT",
            sysdir / "devices" / "virtual" / "powercap" / "intel-rapl",
        ),
        patch(
            "coolstep.compat.detect._PLATFORM_PROFILE",
            sysdir / "firmware" / "acpi" / "platform_profile",
        ),
        patch("coolstep.compat.detect._EFI_VARS", sysdir / "firmware" / "efi" / "efivars"),
        patch("coolstep.compat.detect._read", side_effect=fake_read),
        patch("coolstep.compat.detect.shutil.which", side_effect=fake_which),
        patch("coolstep.compat.detect.subprocess.run", side_effect=fake_run),
        patch("coolstep.compat.detect.importlib.util.find_spec", side_effect=fake_find_spec),
        patch("coolstep.compat.detect._service_active", side_effect=fake_active),
        patch("coolstep.compat.detect.os.uname", return_value=os.uname_result(
            ("Linux", "test", "6.0", "#1", snap.uname_machine),
        )),
        patch.dict(os.environ, env_overlay, clear=True),
    ]
    # power_supply path read inside detect_caps — patch the constructor used there
    # detect.py uses `Path("/sys/class/power_supply")` inline; we need a different patch
    # strategy: redirect Path construction is too invasive, instead monkey-patch
    # the Path class usage via a wrapper.
    # detect.py: has_battery = Path("/sys/class/power_supply").is_dir() ...
    # We'll patch via a custom Path wrapper through `coolstep.compat.detect.Path`.
    fake_power_supply = sysdir / "class" / "power_supply"
    real_path = __import__("coolstep.compat.detect", fromlist=["Path"]).Path
    fake_home = Path(env_overlay["HOME"])

    def _redirecting_path(*args, **kwargs):
        p = real_path(*args, **kwargs)
        s = str(p)
        redirects = {
            "/sys/class/power_supply": fake_power_supply,
            "/sys/class/thermal": sysdir / "class" / "thermal",
            "/etc/os-release": tmp_root / "etc" / "os-release",
            "/proc/cpuinfo": tmp_root / "proc" / "cpuinfo",
            "/proc/sys/kernel/perf_event_paranoid": (
                tmp_root / "proc" / "sys" / "kernel" / "perf_event_paranoid"
            ),
            "/sys/class/dmi/id/sys_vendor": sysdir / "class" / "dmi" / "id" / "sys_vendor",
            "/sys/class/dmi/id/product_name": sysdir / "class" / "dmi" / "id" / "product_name",
            "/sys/class/dmi/id/chassis_vendor": sysdir / "class" / "dmi" / "id" / "chassis_vendor",
        }
        if s in redirects:
            return redirects[s]
        if s.startswith("/run/user/"):
            return tmp_root / s.lstrip("/")
        return p

    _redirecting_path.home = staticmethod(lambda: fake_home)  # type: ignore[attr-defined]
    _redirecting_path.cwd = staticmethod(real_path.cwd)  # type: ignore[attr-defined]
    patches.append(patch("coolstep.compat.detect.Path", _redirecting_path))

    # Default-arg patching: detect_caps(*, cpu_root=_CPU_ROOT) and
    # _epp_state(cpu_root: Path = _CPU_ROOT) capture the module-level
    # _CPU_ROOT at def-time. Patching the module attribute does not change
    # the function's frozen default — we must rewrite __kwdefaults__ /
    # __defaults__ for the duration of the test.
    import coolstep.compat.detect as _det
    patched_cpu_root = sysdir / "devices" / "system" / "cpu"
    saved_caps_kw = _det.detect_caps.__kwdefaults__
    saved_epp_def = _det._epp_state.__defaults__
    _det.detect_caps.__kwdefaults__ = {"cpu_root": patched_cpu_root}
    _det._epp_state.__defaults__ = (patched_cpu_root,)

    # Apply
    started = []
    try:
        for p in patches:
            started.append(p.__enter__())
        from coolstep.compat import reset_caps
        reset_caps(None)
        yield tmp_root
    finally:
        for p in reversed(patches):
            try:
                p.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        _det.detect_caps.__kwdefaults__ = saved_caps_kw
        _det._epp_state.__defaults__ = saved_epp_def
        from coolstep.compat import reset_caps
        reset_caps(None)
