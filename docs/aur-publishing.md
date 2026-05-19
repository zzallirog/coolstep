# AUR Publishing — Readiness & Gotchas

> Living document — обновляемый при появлении новых кейсов.
> First scan: pre-AUR-cut sandbox test, 2026-05-12.
> All four known gotchas closed at commit `ae273d7`.

## TL;DR

Прежде чем `git push aur master` — **обязательно** прогнать полный
`makepkg → install → run` цикл в **чистом Arch** (LXC / Docker / VM).

AUR — анархия. Никто там не QA-тит за тебя. Поэтому единственный QA-gate
PKGBUILD — это **твой собственный** sandbox-test до push'а. Ошибка
PKGBUILD'а попадает сразу к user'ам, и они её увидят как
«*makepkg падает*» в комментариях AUR-страницы.

Этот документ — две вещи:
1. **Methodology** — как мы тестим (sandbox playbook).
2. **Gotchas** — 4 кейса найденных в первом cycle, каждый с deep dive.

И две FAQ-секции в конце — для **user'ов** (тех кто ставит coolstep) и
для **developer'ов** (тех кто пилит / релизит coolstep).

---

## Methodology — как ловить такие баги

Минимальный AUR sandbox = чистый Arch без твоих dev-deps в виду.

### Стек

| | LXC (recommended) | Docker | VM |
|---|---|---|---|
| Setup time | ~30 sec | ~10 sec | ~3 min |
| Teardown | instant (`pct destroy`) | instant (`docker rm`) | ~30 sec |
| Hardware probe (`coolstep adapters`) | ✓ realistic | ⚠ partial sysfs | ✓ |
| Systemd user units test | ✓ via linger | ⚠ требует `--init` | ✓ |
| Cost | 2 cores / 2GB / 6GB disk | minimal | overhead |

LXC unprivileged + `nesting=1` + `keyctl=1` — это sweet spot.

### Playbook (~5 минут от zero до verdict)

```bash
# 1. create container
pct create 999 NVME500:vztmpl/archlinux-base_*.tar.zst \
  --hostname coolstep-aur-test \
  --rootfs NVME500:6 \
  --cores 2 --memory 2048 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1 --features nesting=1,keyctl=1 \
  --password=throwaway --start 1

# 2. setup (см. Gotcha-0 про sandbox flag)
pct exec 999 -- pacman -Sy --disable-sandbox --noconfirm
pct exec 999 -- pacman -S --disable-sandbox --noconfirm --needed \
  base-devel git python sudo debugedit \
  python-fastapi uvicorn python-pydantic python-click python-psutil \
  python-build python-installer python-setuptools python-wheel \
  python-pytest python-pytest-cov python-freezegun python-httpx

pct exec 999 -- useradd -m -G wheel tester
pct exec 999 -- bash -c 'echo "tester ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/99'

# 3. push PKGBUILD + build
pct push 999 packaging/aur/coolstep/PKGBUILD /home/tester/coolstep/PKGBUILD
pct exec 999 -- chown -R tester:tester /home/tester/coolstep
pct exec 999 -- sudo -u tester bash -c \
  'cd /home/tester/coolstep && makepkg --printsrcinfo > /dev/null && namcap PKGBUILD'
pct exec 999 -- sudo -u tester bash -c \
  'cd /home/tester/coolstep && makepkg --noconfirm'   # ← реальный test

# 4. install + smoke
pct exec 999 -- pacman -U --disable-sandbox --noconfirm \
  /home/tester/coolstep/*.pkg.tar.zst
pct exec 999 -- coolstep doctor
pct exec 999 -- coolstep adapters --json

# 5. teardown
pct stop 999 && pct destroy 999
```

### Что верифицируем (chiklist)

- [ ] `makepkg --printsrcinfo` parses (PKGBUILD syntactically valid)
- [ ] `namcap PKGBUILD` без warnings
- [ ] `makepkg --noconfirm` → exit 0 AND `.pkg.tar.zst` создан
- [ ] `pacman -Ql coolstep` показывает **все** ожидаемые service/timer units
- [ ] `pacman -Qi coolstep` Version == git tag == pyproject.toml version == `python -c 'from coolstep import __version__'`
- [ ] `/usr/bin/coolstep --help` отвечает
- [ ] `coolstep doctor` запускается (verdict critical OK на fresh install — daemon не запущен)
- [ ] `coolstep adapters` показывает realistic collectors для test platform
- [ ] **`coolstep-dashboard` + `curl http://127.0.0.1:18889/`** возвращает **200** (не 500) — guards G-6 (static assets in wheel)
- [ ] `pacman -Ql coolstep | grep static/` — non-empty (G-6 belt-and-suspenders)
- [ ] **SSH tunnel test** — `ssh -L 18889:127.0.0.1:18889 host` + `curl http://localhost:18889/api/health` локально → 200. Подтверждает enterprise/headless reachability pattern.

---

## Gotchas — case studies (found 2026-05-12)

### G-0 — pacman sandbox требует Landlock kernel feature (LXC/container case)

**Symptom (sandbox-side, не у AUR-user'а):**
```
error: restricting filesystem access failed because Landlock is not supported by the kernel!
error: switching to sandbox user 'alpm' failed!
warning: failed to retrieve some files
```
И самое подлое — pacman **возвращает exit 0** при этой ошибке (известный
upstream bug). `pacman -S pkg` → exit 0, но `pacman -Q pkg` → «package not
found». Тихая поломка.

**Root cause:** pacman 7.0+ использует Landlock LSM для sandbox'а
downloader-процесса. PVE / Docker / minimal-kernel hosts часто без
Landlock в configа. На normal Arch desktop оно есть, поэтому on-machine
test не ловит проблему.

**Fix (sandbox-side):** Каждый pacman call с `--disable-sandbox`:
```bash
pacman -S --disable-sandbox --noconfirm ...
```

**For AUR users:** **NOT а user-facing bug.** У них normal Arch с
Landlock — sandbox работает. Это только проблема **dev sandbox'а**.

**Why this matters anyway:** Это пример того, что *среда теста ≠ среда
production*. Если тестим в LXC — это **другая** среда чем у юзера. Может
скрыть баги, может показать ложно-positive ошибки. **Знай свою test env.**

---

### G-1 — pytest fails в `check()`: `httpx` missing

**Symptom (user-side):**
```
==> Starting check()...
E   ModuleNotFoundError: No module named 'httpx'
==> ERROR: A failure occurred in check().
    Aborting...
```

**Root cause:** `fastapi.testclient` (используется в
`tests/dashboard/test_routes.py`) импортирует `httpx` runtime. fastapi
list'ит httpx как **peer dep** — pip-installed fastapi его автоматически
ставит. **pacman этого не делает** — package manager видит только то,
что explicit в depends. У pacman нет понятия optional peer dep.

PKGBUILD `makedepends` включал pytest + freezegun, но не httpx —
поэтому в `check()` phase test fail.

**Fix (commit `ae273d7`):** Новая секция `checkdepends=()` в обоих
PKGBUILD:
```
checkdepends=(
  'python-pytest'
  'python-pytest-cov'
  'python-freezegun'
  'python-httpx'    # fastapi.testclient requirement
)
```

`checkdepends` — это специальная секция makepkg для test-only deps. Они
ставятся перед `check()` и не считаются runtime deps пакета.

**Why missed:** На dev-машине coolstep ставится через `pip install` или
`uv add`. Pip тянет httpx как transitive через `mcp`, `anyio`, или сам
fastapi. Когда мы переходим на pacman-style — transitive deps падают,
надо list'ить явно.

**CI guard (TODO):** `tests/test_packaging.py`:
```python
import re, sys
from pathlib import Path

def _extracted_imports(test_dir: Path) -> set[str]:
    imports = set()
    for f in test_dir.rglob("*.py"):
        for line in f.read_text().splitlines():
            m = re.match(r"^(?:from|import)\s+(\w+)", line.strip())
            if m and m.group(1) not in sys.stdlib_module_names:
                imports.add(m.group(1))
    return imports

def test_checkdepends_covers_test_imports():
    imports = _extracted_imports(Path("tests"))
    pkgbuild = Path("packaging/aur/coolstep/PKGBUILD").read_text()
    for imp in imports - {"coolstep", "pytest", "fastapi", ...}:
        assert f"python-{imp.replace('_','-')}" in pkgbuild, \
            f"checkdepends missing python-{imp}"
```

---

### G-2 — `__version__` drift (`0.0.1` vs `0.5.0`)

**Symptom (user-side):**
```
$ pacman -Qi coolstep | grep Version
Version : 0.5.0-1

$ python -c "from coolstep import __version__; print(__version__)"
0.0.1                          # ← wat
```

**Root cause:** Три источника правды about version:
1. `pyproject.toml` `[project] version = "0.5.0"` — setuptools/pip authoritative
2. `coolstep/__init__.py` `__version__ = "0.0.1"` — Python runtime view
3. `packaging/aur/*/PKGBUILD` `pkgver=0.5.0` — pacman view

`__init__.py` был забыт при bump'е. На `pip install -e .` это не видно
(setuptools читает pyproject), но `from coolstep import __version__`
читает строку из `__init__.py` — она статическая.

**Fix (commit `ae273d7`):** Sync `coolstep/__init__.py` → `0.5.0`.

**Why missed:** `scripts/release.sh` bump'ит pyproject и git tag, но не
`__init__.py`. На pip-installed editable mode (`pip install -e .`) баг
менее заметен — `__version__` всё равно 0.0.1 но никто эту строку не
дёргает.

**CI guard (TODO):** `tests/test_version_sync.py`:
```python
import tomllib
from pathlib import Path
from coolstep import __version__

def test_version_synced_with_pyproject():
    with open("pyproject.toml", "rb") as f:
        pyproject_version = tomllib.load(f)["project"]["version"]
    assert __version__ == pyproject_version, (
        f"__init__.py says {__version__}, "
        f"pyproject.toml says {pyproject_version}"
    )
```

**Release script fix (TODO):** В `scripts/release.sh` добавить:
```bash
sed -i "s/^__version__ = .*/__version__ = \"$VERSION\"/" coolstep/__init__.py
python -c "from coolstep import __version__; assert __version__ == \"$VERSION\""
```

---

### G-3 — `coolstep-bench-gc.{service,timer}` not installed

**Symptom (user-side):** No error, no warning. Просто:
```
$ systemctl --user list-timers | grep coolstep
# (empty)

$ ls /usr/lib/systemd/user/coolstep-*
/usr/lib/systemd/user/coolstep-collector.service
/usr/lib/systemd/user/coolstep-dashboard.service
# coolstep-bench-gc.service/.timer missing
```
И через несколько недель `bench/runs/` начинает забивать диск, потому
что weekly GC не запускается.

**Root cause:** `systemd/CLAUDE.md` документирует **4** unit'а в репо:
- `coolstep-collector.service`
- `coolstep-dashboard.service`
- `coolstep-bench-gc.service`
- `coolstep-bench-gc.timer`

PKGBUILD `package()` функция явно install -Dm644'ит только первые
**два**. Bench-GC unit'ы существовали в `systemd/` директории, но
никогда не попадали в `.pkg.tar.zst`.

**Fix (commit `ae273d7`):** В обоих PKGBUILD `package()`:
```
install -Dm644 systemd/coolstep-bench-gc.service \
  "$pkgdir/usr/lib/systemd/user/coolstep-bench-gc.service"
install -Dm644 systemd/coolstep-bench-gc.timer \
  "$pkgdir/usr/lib/systemd/user/coolstep-bench-gc.timer"
```

**Why missed:** Bench-gc добавлен **позже** initial PKGBUILD'а (после
P0+1 ship). При добавлении unit'а в `systemd/` забыли обновить PKGBUILD
package() — это manual sync step без CI guard.

**CI guard (TODO):** `tests/test_packaging.py`:
```python
from pathlib import Path

def test_pkgbuild_installs_all_systemd_units():
    repo_units = (
        list(Path("systemd").glob("coolstep-*.service")) +
        list(Path("systemd").glob("coolstep-*.timer"))
    )
    assert repo_units, "no units found in systemd/ — check path"
    for pkgbuild_path in [
        "packaging/aur/coolstep/PKGBUILD",
        "packaging/aur/coolstep-git/PKGBUILD",
    ]:
        pkgbuild = Path(pkgbuild_path).read_text()
        for unit in repo_units:
            assert unit.name in pkgbuild, (
                f"{pkgbuild_path} doesn't install {unit.name}"
            )
```

---

### G-4 — `install-units` blindly suggests `systemctl --user` on headless

**Symptom (user-side, на rack-server / SSH login без graphical):**
```
$ coolstep install-units
  coolstep-collector.service: written
  coolstep-dashboard.service: written

Next steps:
  systemctl --user daemon-reload
  systemctl --user enable --now coolstep-collector coolstep-dashboard
  xdg-open http://127.0.0.1:18889/

$ systemctl --user daemon-reload
Reload daemon failed: Process org.freedesktop.systemd1 exited with status 1
$ sudo systemctl --user daemon-reload
Failed to connect to user scope bus via local transport:
  $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined
```

**Root cause:** На headless server (SSH login без graphical session)
systemd **user manager не стартует автоматически**. Поэтому:
- `/run/user/$UID` не создаётся
- `$XDG_RUNTIME_DIR` не установлен
- dbus user bus не работает
- `systemctl --user` не находит куда подключаться

Это **базовый Linux behavior**, не coolstep bug. Но `install-units`
команда выводила universal instructions без проверки — и user'ы видели
красные ошибки.

**Fix (commit `ae273d7`, `coolstep/inspect/cli.py`):**
```python
xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
has_user_session = bool(xdg_runtime) and Path(xdg_runtime).exists()

if has_user_session:
    # обычные инструкции
    ...
else:
    click.echo("⚠ No systemd user session detected ...")
    click.echo("One-time setup (then re-login OR export var manually):")
    click.echo("  sudo loginctl enable-linger $USER")
    click.echo(f"  export XDG_RUNTIME_DIR=/run/user/{os.getuid()}")
    ...
```

**Why missed:** Dev и pilot deployments были на desktop (Hyprland) — там
`$XDG_RUNTIME_DIR` установлен из коробки. Headless test случай (rack
server, SSH login) попал в pilot позже, при тесте на headless хосте.

**CI guard (TODO):**
```python
def test_install_units_headless(monkeypatch, capsys):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    from click.testing import CliRunner
    from coolstep.inspect.cli import main
    result = CliRunner().invoke(main, ["install-units", "--force"])
    assert "loginctl enable-linger" in result.output
```

---

### G-5 — `status=226/NAMESPACE` restart loop: `~/coolstep/data/` doesn't exist

**Symptom (user-side):**
```
$ systemctl --user status coolstep-dashboard
   Active: activating (auto-restart) (Result: exit-code)
  Process: ExecStart=... (code=exited, status=226/NAMESPACE)
   Mem peak: 2M     CPU: 3ms

$ journalctl --user -u coolstep-dashboard -n 5
... Failed to set up mount namespacing: /home/<user>/coolstep/data: No such file or directory
... Failed at step NAMESPACE spawning /usr/bin/env: No such file or directory
... Main process exited, code=exited, status=226/NAMESPACE
... Scheduled restart job, restart counter is at 45.
```

Daemon крутится в restart loop ~5 sec interval. `curl :18889` → `Connection refused`.

**Root cause:** Unit template имеет `ReadWritePaths=%h/coolstep/data`.
systemd при setup mount namespace **требует**, чтобы все `ReadWritePaths=`
пути существовали на момент start. Если директория не создана —
namespacing fails ДО того как ExecStart Python запустится. Это поэтому
`Mem peak: 2M, CPU: 3ms` — процесс ни разу даже не родился, systemd
убил namespacing setup'ом.

Сообщение `Failed at step NAMESPACE spawning /usr/bin/env: No such
file or directory` — обманчивое. На самом деле `/usr/bin/env`
существует; «No such file or directory» относится к `%h/coolstep/data`,
но systemd форматирует error confusingly.

**Fix (commit TBD — следующий после ae273d7):** `install_units.install()`
теперь сам создаёт `~/coolstep/data/` перед записью unit'ов:
```python
def _data_dir() -> Path:
    return Path.home() / "coolstep" / "data"

def install(force=False):
    data = _data_dir()
    if not data.exists():
        data.mkdir(parents=True, exist_ok=True)
    ...
```

**Manual fix (для users на pre-patch версии):**
```bash
mkdir -p ~/coolstep/data
systemctl --user reset-failed coolstep-collector coolstep-dashboard
systemctl --user restart coolstep-collector coolstep-dashboard
```
`reset-failed` нужен потому что restart counter мог накопиться до
limit'а и systemd ввёл backoff / hold.

**Why missed:** На dev-машине `~/coolstep/data/` существует естественно
(репо checked out в `~/coolstep`, data/ создаётся первым frame'ом).
В AUR install (где репо не checked out) и `pipx install coolstep`
(где user никогда не видел src tree) — этой директории нет, и
install-units её не создавал.

**Why message confusing:** systemd error formatter показывает имя
последнего spawn-target'а (`/usr/bin/env`), но ошибка пришла от
namespacing setup на `ReadWritePaths=`. Это open bug в systemd
error messages — не наша вина, но user-confusing.

**CI guard (TODO):**
```python
def test_install_units_creates_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from coolstep.inspect.install_units import install, _data_dir
    install(force=True)
    assert _data_dir().exists()
    assert _data_dir().is_dir()
```

---

### G-6 — `static/` assets missing from wheel — `/` returns 500 (`/api/*` OK)

**Symptom (user-side, SSH tunneled или local):**
```
$ curl http://127.0.0.1:18889/api/health
{"daemon_seen":true,"store_path":"...","store_size_bytes":4096,...}

$ curl http://127.0.0.1:18889/
Internal Server Error
```
Все API endpoint'ы (`/api/*`) ОТВЕЧАЮТ 200. Падает **только** `/`
(главная HTML страница). Journal:
```
RuntimeError: File at path .../coolstep/dashboard/static/index.html does not exist.
```

**Root cause:** `pyproject.toml` `[tool.setuptools.package-data]`
listed только `coolstep.compat = ["core.json"]`. `coolstep/dashboard/static/`
содержит 36 файлов (HTML, CSS, JS, Lit components, i18n bundle) — но
setuptools НЕ включает их в wheel automatically, потому что:
- `find` packages в `[tool.setuptools.packages.find]` находит **только**
  Python модули (где есть `__init__.py`)
- `static/` — не Python module, это data files
- Без явной enumeration в `package-data` — они NOT packaged

На dev-машине (где coolstep run'ится **из git checkout**) static/
file существует в репе → dashboard finds via relative path → 200 OK.
После `pip install --user .` → wheel built → static/ потерян → 500.

**Pacman/AUR — same bug:** PKGBUILD `build()` зовёт `python -m build
--wheel`, который вызывает то же setuptools → static/ не попадает в
wheel → не попадает в `$pkgdir` через installer → 500 у AUR-юзера тоже.

**Fix (commit TBD):** Расширить `package-data`:
```toml
[tool.setuptools.package-data]
"coolstep.compat" = ["core.json"]
"coolstep.dashboard" = [
    "static/*.html",
    "static/*.css",
    "static/*.js",
    "static/components/*.js",
    "static/pills/*.js",
    "static/i18n/*.json",
    "static/i18n/*.js",
    "static/lib/*",
]
```

**Why missed:** Это **самый критичный** finding серии, и **methodology
gap** в моём LXC test playbook. На CT 211/212 я curl'ил `/api/health`,
`/api/adapters` — оба 200. На `/` (HTML) я **не** curl'ил.

`coolstep doctor` тоже не проверяет `/`. Он проверяет `dashboard_api`
через `/api/health`, не root path. Поэтому doctor говорит ✓ дашборд
доступен — а на самом деле HTML страница 500'ит.

**Detection — обновляю chiklist:**
- `curl http://127.0.0.1:18889/` → 200 (не 500)
- `pacman -Ql coolstep | grep static/` → non-empty list
- Доктор должен научиться probe'ить `/`, не только `/api/health`
  (filed как followup TODO)

**Enterprise/headless implication:** Этот bug особенно ядовит для
**SSH-tunnel access pattern** — типичного enterprise / headless server
deployment:
```
admin laptop                       rack server
─────────────                      ─────────────
ssh -L 18889:127.0.0.1:18889 srv → coolstep-dashboard --host 127.0.0.1
browser http://localhost:18889/ ──→ tunnel ──→ 500 (HTML missing)
```
Admin не имеет доступа на сервер кроме SSH. Без HTML страницы —
дашборд для него фактически не существует. API только через curl
вручную — degraded UX.

Поэтому coolstep на server segment **требует** этот fix. До G-6
исправления headless deployment был только частично работоспособен.

**CI guard (TODO):**
```python
def test_dashboard_static_files_packaged():
    """G-6 guard — static/ assets must be in wheel."""
    import coolstep.dashboard
    pkg_root = Path(coolstep.dashboard.__file__).parent
    static = pkg_root / "static"
    assert static.exists(), f"static/ missing from package at {static}"
    assert (static / "index.html").exists()
    js_files = list(static.rglob("*.js"))
    assert len(js_files) >= 10, f"too few JS files: {js_files}"

def test_dashboard_root_returns_200(test_client):
    """Smoke — `/` HTML страница серверится (не 500)."""
    response = test_client.get("/")
    assert response.status_code == 200
    assert b"<html" in response.content.lower()
```

---

### G-9 — vendor-locked CPU temp shortcut (AMD-only) — Intel hosts wrote `NULL` to sqlite

**Symptom (Intel host, especially after G-7 lands):**

User looks at dashboard, sees `CPU TCTL → N/A` and a hover tooltip from
G-7 saying «CPU package temp not exposed by this platform».  But on her
Intel i5-13500 / Debian, the temp **is** there — `cat /sys/class/hwmon/hwmon4/temp1_input`
returns `49000` (= 49.0 °C), and `temp1_label` is `Package id 0`.  G-7 N/A
is **factually wrong** for this host.

The collector emits the per-label dict correctly:
```
cpu.temps_c = {"package": 49.0, "core 0": 48.0, "core 1": 47.0, ...}
```
…and `_normalize_cpu_temps()` even has `"package"` listed as a canonical
alias.  So where does the data get lost?

**Root cause:**

`coolstep/core/store.py:75` — sqlite shortcut row build:
```python
cpu_temp = frame.cpu.temps_c.get("tctl") or frame.cpu.temps_c.get("tdie")
```
Same pattern in `coolstep/core/efficiency_calibration.py:93`.

These are **AMD-only**.  `tctl` and `tdie` are k10temp / zenpower sensor
labels (AMD Ryzen).  On Intel `coretemp` there are no such labels —
`Package id 0` is what you get, normalized to `"package"`.

So on Intel: `get("tctl") or get("tdie")` → `None or None` → `cpu_temp =
None` written to sqlite, even though `temps_c["package"]` was sitting
right there in the same dict with the real 49.0 °C value.

The downstream chain: sqlite shortcut → `/api/telemetry/latest` summary
row → SSE shortcut → frontend `f.cpu_temp == null` → G-7 frontend
patch fires `N/A` branch.  Cascade of «correct given the input», but the
input itself was lost two layers up.

**Fix:**

Both spots, vendor-agnostic fallback chain:
```python
cpu_temp = (
    frame.cpu.temps_c.get("tctl")        # AMD Ryzen primary
    or frame.cpu.temps_c.get("tdie")     # AMD Ryzen secondary
    or frame.cpu.temps_c.get("package")  # Intel coretemp "Package id 0"
    or (max(frame.cpu.temps_c.values()) if frame.cpu.temps_c else None)
)
```

Last clause is a universal fallback — if a platform happens to label
neither tctl/tdie/package (theoretical ARM SoC, BMC sensor with a custom
label), the hottest reading still surfaces.  Never silently `None` when
data exists.

**Why missed:**

Dev hardware (ASUS TUF A15, Ryzen 9 7940HS) has Tctl in `temps_c` always.
On that machine the shortcut works on first sample.  Synthetic compat
fixtures cover Intel (`thinkpad_x1_carbon_i7_intel`,
`intel_desktop_homelab_i5_13500`) but the harness exercises **detection
and install-plan**, not the sqlite shortcut path.  Until G-9, no test
asserted «cpu_temp column is non-null after writing an Intel-style frame».

**Interplay with G-7:**

G-7 (frontend explanatory N/A) is **correct as policy** — null at the wire
should render «not applicable here» rather than silent `—`.  But G-9 is
the upstream collector/store bug that was making the wire null when it
shouldn't have been.  After G-9 fix, Intel hosts will see the real number
(49°C, climbing under load); G-7 N/A still fires correctly for the
genuinely-absent fields (Intel iGPU power, no-fan-tool RPM, RAPL locked).

**CI guard:** `tests/test_cpu_temp_fallback.py` — 6 cases covering AMD
primary/secondary, Intel package, universal max-fallback, empty-dict
None, and end-to-end via `Store.write_frame()` against a fresh sqlite.

---

## FAQ — for users (installing coolstep from AUR)

### Q: `makepkg -si` падает с `ModuleNotFoundError: httpx`
A: Bug в pre-`ae273d7` PKGBUILD. Workaround: `sudo pacman -S
python-httpx` перед `makepkg`. Или `makepkg --nocheck` чтобы пропустить
тесты (не рекомендуется — нет верификации что билд actually работает).
Long-term fix: обнови `coolstep-git` после `ae273d7` — `checkdepends`
там корректный.

### Q: `pacman -Qi coolstep` показывает 0.5.0, а `python -c 'import coolstep; print(coolstep.__version__)'` показывает 0.0.1
A: Bug в pre-`0.5.0+1` release. Bump до post-`ae273d7` версии — drift
закрыт.

### Q: Weekly bench/runs/ cleanup не запускается, диск растёт
A: Проверь `systemctl --user list-timers | grep coolstep`. Если
`coolstep-bench-gc.timer` отсутствует — старый PKGBUILD без bench-gc
install (`< ae273d7`). Manual fix до апгрейда:
```bash
# взять unit-файлы из репо
curl -L https://github.com/zzallirog/coolstep/raw/master/systemd/coolstep-bench-gc.service \
  | sudo tee /usr/lib/systemd/user/coolstep-bench-gc.service
curl -L https://github.com/zzallirog/coolstep/raw/master/systemd/coolstep-bench-gc.timer \
  | sudo tee /usr/lib/systemd/user/coolstep-bench-gc.timer
systemctl --user daemon-reload
systemctl --user enable --now coolstep-bench-gc.timer
```

### Q: `systemctl --user enable coolstep-collector` падает с `Failed to connect to user scope bus`
A: Headless server (SSH без графической сессии)? Это **Linux behavior**,
не coolstep. Делай:
```bash
sudo loginctl enable-linger $USER
# log out + back in, ИЛИ для текущей shell:
export XDG_RUNTIME_DIR=/run/user/$(id -u)
```
После `enable-linger` user manager стартует при boot — больше не
понадобится export XDG_RUNTIME_DIR при каждом SSH (после следующего
login).

post-`ae273d7` версии `coolstep install-units` сам это диагностирует и
выводит правильные команды.

### Q: Dashboard на :18889 не отвечает
A:
```bash
systemctl --user status coolstep-dashboard    # running?
journalctl --user -u coolstep-dashboard -n 30  # last 30 lines
ss -tlnp | grep 18889                          # порт занят чем-то ещё?
```
Most common scenarios:
- **`status=226/NAMESPACE` + `Failed to set up mount namespacing`** —
  G-5, `~/coolstep/data/` missing. Fix: `mkdir -p ~/coolstep/data &&
  systemctl --user reset-failed coolstep-* && systemctl --user
  restart coolstep-collector coolstep-dashboard`
- **port collision** — что-то ещё на 18889. `ss -tlnp | grep 18889`
- **daemon crashed at startup** — см. journal Python traceback

### Q: `/api/*` всё работает (200), но `/` даёт 500
A: G-6 — `static/` не попал в wheel. Симптом в journal:
```
RuntimeError: File at path .../coolstep/dashboard/static/index.html does not exist.
```
Это packaging bug в pre-`pyproject-static-fix` версии. Pip-installed
wheel'а static/ assets отсутствуют. Fix: апгрейд coolstep
(post-`pyproject` fix commit) — `pip install --user --force-reinstall coolstep`
или `pipx reinstall coolstep`. Если ставила из git src — `git pull &&
pip install --user --force-reinstall .`

### Q: `coolstep doctor` показывает `verdict: ✗ critical` на свежем install
A: Это **нормально** для fresh install до того как daemon побежал. После
`systemctl --user start coolstep-collector` подожди ~5 sec и повтори —
доктор должен перейти к ✓ или ⚠. Если остался critical — копай в
`journalctl`.

### Q: Я на не-Arch (Debian/Ubuntu/Fedora) — как ставить?
A: AUR — только Arch. Для других distro:
```bash
pipx install coolstep              # или
uv tool install coolstep           # или
pip install --user coolstep
coolstep install-units             # пишет unit'ы в ~/.config/systemd/user/
```
Не забудь то же linger'ить если headless.

---

## FAQ — for developers (publishing coolstep)

### Q: Как тестить PKGBUILD до push на AUR?
A: См. секцию «Methodology» выше. TL;DR — LXC sandbox, 5 минут, чистый
Arch. На своей dev-машине **не показателен** — там pip-deps скрывают
PKGBUILD gaps.

### Q: Почему `--disable-sandbox` нужен в LXC, но не на моей машине?
A: PVE / Docker / minimal kernels часто без Landlock LSM. Twoя normal
Arch desktop с ним. См. G-0. Если test'ишь в env с Landlock — флаг не
нужен.

### Q: Когда обновлять `__version__` (три места sync)?
A: При каждом version bump синхронизировать **все три**:
1. `pyproject.toml` `[project] version`
2. `coolstep/__init__.py` `__version__`
3. `packaging/aur/coolstep/PKGBUILD` `pkgver`
4. `CHANGELOG.md` `## [X.Y.Z] — YYYY-MM-DD`
5. git tag `vX.Y.Z`

CI guard в `tests/test_version_sync.py` (см. G-2) ловит drift между
(1) и (2). PKGBUILD vs git tag — manual review.

Можно автоматизировать через bump-script — патч release.sh:
```bash
sed -i "s/^__version__ = .*/__version__ = \"$VERSION\"/" coolstep/__init__.py
sed -i "s/^pkgver=.*/pkgver=$VERSION/" packaging/aur/coolstep/PKGBUILD
```

### Q: Что нужно проверить в PKGBUILD перед `git push aur`?
A: chiklist из секции «Methodology» (выше). И:
- `makepkg --printsrcinfo > .SRCINFO` — обязательно для AUR (commit
  `.SRCINFO` along with PKGBUILD)
- `namcap PKGBUILD` — clean (no warnings)
- `namcap *.pkg.tar.zst` (post-build) — clean

### Q: Как mock'нуть headless env в test?
A:
```python
def test_install_units_headless(monkeypatch, capsys):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    from click.testing import CliRunner
    from coolstep.inspect.cli import main
    result = CliRunner().invoke(main, ["install-units", "--force"])
    assert "loginctl enable-linger" in result.output
    assert "XDG_RUNTIME_DIR" in result.output
```

### Q: Когда стоит делать `-git` PKGBUILD vs только stable?
A:
- **stable** (`coolstep/PKGBUILD`) — для каждого release tag.
  `source=tarball/v$pkgver.tar.gz`, deterministic build.
- **`-git`** (`coolstep-git/PKGBUILD`) — для users которые хотят
  cutting-edge. `source=git+master`, `pkgver()` функция считает rev
  count. Conflicts с stable.

Оба нужны: stable — для большинства, `-git` — для early adopters /
тестеров.

### Q: Что добавить в CI чтобы такого не повторилось?
A: 3 PR'а в tests/:

1. `tests/test_version_sync.py` (G-2 guard) — sync `__version__` ↔
   pyproject
2. `tests/test_packaging.py::test_pkgbuild_installs_all_units` (G-3
   guard) — все unit'ы в repo попадают в PKGBUILD package()
3. `tests/test_packaging.py::test_checkdepends_covers_test_imports` (G-1
   guard) — все test imports listed в checkdepends

Все три — pure Python, без LXC, без хардвара. Запускаются в CI каждый
push. Cost: ~50 LOC, ~0.5 sec runtime.

Плюс — **manual** LXC test перед каждым AUR push. Это полный
end-to-end, ловит G-0 (sandbox), G-4 (headless instructions),
package() reality, и все будущие unknowns.

### Q: Как обновить уже опубликованный AUR пакет?
A:
```bash
git clone ssh://aur@aur.archlinux.org/coolstep-git.git aur-mirror
cd aur-mirror
cp ../packaging/aur/coolstep-git/PKGBUILD .
makepkg --printsrcinfo > .SRCINFO
git add PKGBUILD .SRCINFO
git commit -m "bump to X.Y.Z"
git push
```
AUR sees commit, рендерит новую страницу. Users `paru -Syu` / `yay
-Syu` подтянут.

---

## Pointers

- `packaging/aur/coolstep/PKGBUILD` — stable PKGBUILD
- `packaging/aur/coolstep-git/PKGBUILD` — git-master PKGBUILD
- `packaging/aur/README.md` — AUR upload workflow
- `scripts/release.sh` — version bump pipeline
- `systemd/CLAUDE.md` — список всех unit'ов в репо
- `coolstep/inspect/install_units.py` — embedded unit templates для
  non-AUR installs (pipx / pip / uv)

---

**Last updated:** 2026-05-12
**Cases first scanned:** CT 211/212 LXC sandbox on PVE, 2026-05-12
**Patch commit:** `ae273d7` (`AUR findings fix + install-units headless detect`)
