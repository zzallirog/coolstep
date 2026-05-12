# Contributing to coolstep

Two-line PRs are welcome. Coolstep is intentionally small-scope; we say no to
features that don't serve **one of the two deployment targets** (desktop responsiveness or
server foresight).

## What we accept readily

- **New hardware support** — add an entry to `coolstep/compat/core.json` +
  a smoke test that doesn't depend on the actual chip. Example:
  ```json
  // core.json
  "hwmon": { "fan_drivers": [..., "your_new_chip"] }
  ```
- **New community pointer** — same file, `community_pointers` array.
  Include `id`, `trigger`, `reason`, per-distro `commands`, upstream `url`.
- **Distro support** — extend `distro.id_to_clan` + `clan_pkg_manager` in
  `core.json`.
- **Test additions** for existing collectors.
- **Documentation fixes** — typos, missing files, broken links.

## What needs design discussion first (open an issue)

- New collector or actuator → file a design issue with: target platform,
  expected `signals()` manifest, permission requirements, dry-run plan.
- New top-level config key → discuss merge semantics + L1/L2 impact.
- Anything that changes `PlatformCaps` schema → frozen dataclass, breaks
  consumers.
- Anything that touches the daemon main loop or KNN predictor → high blast
  radius.

## What we say no to

- Cross-platform GUIs (we have a web dashboard; that's it).
- Replacing community tools we already compose with (asusctl, ryzenadj, etc.).
- Cloud sync / telemetry / model upload — privacy-first is non-negotiable.
- Aggressive defaults — `COOLSTEP_ACTUATOR_ENABLE=false` stays the default
  forever.
- Vendored binary blobs — pip / AUR optdeps only.

## Coding conventions

- **Bash:** POSIX `[ ... ]`, quote everything, `fd`/`rg`/`eza`, `set -euo pipefail`.
- **Python:** ruff defaults + type hints. No unused imports. `json.load(file_obj)`
  not `json.loads(file_obj.read())`. See top-level `~/CLAUDE.md` in this repo.
- **KISS:** three repeated lines beat a premature abstraction. No speculative
  abstractions. Bug fix doesn't need surrounding cleanup.
- **Tests:** every new public function gets a test. Test the boundary, not
  the implementation.
- **Comments:** default = none. Only when the WHY is non-obvious (hidden
  constraint, workaround for known bug). Don't restate WHAT the code does.

## Workflow

```bash
git clone https://github.com/zzallirog/coolstep
cd coolstep
pip install --user -e ".[dev]"
make hooks                     # installs pre-commit (ruff + fast pytest)
pytest -m "not slow" -q        # fast suite (~2s, 600+ tests)
ruff check .                   # lint
```

For changes that affect the daemon: also run `pytest --ignore=tests/test_daemon.py`
to catch slow regressions.

## Commit style

- Subject line ≤ 72 chars. Imperative mood. RU or EN both fine.
- Body explains **why**, not **what** (the diff shows what).
- No `Co-Authored-By` trailers; use `git blame` for credit.
- Don't reference issues that close on merge — let GitHub link them.

## Hardware support — quick add path

Found a chip we don't support? Open a PR with these three things:

1. **`coolstep/compat/core.json`** — append driver name to
   `hwmon.fan_drivers` (or wherever it belongs).
2. **A community pointer** — new entry in `community_pointers` with the
   distro install command if the driver needs an out-of-tree DKMS.
3. **One test** — `tests/compat/test_core_json.py` verifying the new name
   is in the loaded manifest.

Merge target: one PR per chip family. Don't bundle five vendors in one PR.

## Maintainer commitments

- PRs that pass CI + don't widen scope merge within 7 days.
- Hardware-support PRs (single-chip additions) merge within 48 h.
- Hostile / off-topic / agenda-pushing PRs close without explanation.
