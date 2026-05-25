# CLAUDE.md — `coolstep/inspect/`

> CLI для интроспекции состояния. Никогда не пишет в hardware, никогда не
> мутирует данные.

**Module version:** 0.2.0
**Last synced with master:** 2026-05-23
**Connectors:**
- ↑ package → `../CLAUDE.md`
- → reads from → `core/store.py`, `adapters/collectors/`

## Subcommands

Discovery / live view:

| Command | Что делает |
|---|---|
| `coolstep adapters [--detailed] [--json]` | Discovery probe — видит ли система collectors + costs |
| `coolstep tail [-n N] [--period S]` | N tick'ов live с прямым sample (без store) |
| `coolstep compat [--json] [--install-plan] [--facet FACET] [--manifest]` | Platform report — что активировалось, что отсутствует, как чинить |

Health / diagnostics:

| Command | Что делает |
|---|---|
| `coolstep doctor [--json]` | 9 структурированных checks (incl. headless `systemd_session` since 2026-05-23) |
| `coolstep drift` | 7 drift indicators (модель устарела) |
| `coolstep efficiency [--since 7d]` | `work_per_degree` sweet spot + knee |
| `coolstep history [--since 24h] [--json]` | Append-only timeline incidents/mode-switches/calibration events |

Store / export:

| Command | Что делает |
|---|---|
| `coolstep stats [--since 24h]` | Aggregate stats (frames, span, temp range, throttle) |
| `coolstep export [--since] [--format csv\|json] [--out -]` | Shortcut columns в csv/json (alias `export-telemetry`) |
| `coolstep export-profile [--out -]` | Dump текущего workload-profile config |
| `coolstep import-profile <path> [--dry-run]` | Apply profile config с validation gate |

Install / introspection (write-able только в `~/.config/`):

| Command | Что делает |
|---|---|
| `coolstep install-units [--force]` | Drop systemd-user unit files (pipx/pip путь) |
| `coolstep predict-debug [--bucket] [--top N] [--json]` | Inspect residual-bank state per-bucket |
| `coolstep predict-replay` | Replay residual log → predicted vs actual trace |

## Invariants

- **Read-only.** Никаких write'ов нигде.
- **Standalone.** Не требует запущенного daemon (кроме `stats`/`export` где
  нужен store). `tail` сам делает discover + sample, work без daemon.
- **Click subcommands.** Не argparse, не typer, не fire — единый стэк.
- **Output stable для grep/awk.** `tail` оneline, `stats` key:value, `export`
  csv/json по флагу. Не менять формат без bump.

## Visit when

- добавляешь команду → `cli.py:@main.command` + этот файл (Subcommands table)
- меняешь output формат → bump version, sync master CLAUDE.md
- появляется write-action команда → переезжает в отдельный entry point
  (`coolstep-admin` или подобное), не в `inspect/`

## FAQ

**Q: Почему `tail` не читает из store?**
A: Чтобы работало без daemon. `tail` — диагностика adapter'ов; store-based
view — это `stats` / `export`.

**Q: Window parser?**
A: `_parse_window()` дублируется в `dashboard/server.py`. Сейчас намеренно —
inspect и dashboard разные lifecycle'ы. Если переедет третий потребитель —
вынести в `core/`.

**Q: Где argument completion?**
A: Click bash/zsh completion работает из коробки: `_COOLSTEP_COMPLETE=fish_source coolstep > completion.fish`. Но это user-side, не нашё дело.

## Sibling pointers

- → `../core/CLAUDE.md` — store, schema (что читаем)
- → `../adapters/collectors/CLAUDE.md` — что probe'ит `adapters` команда
- → `../dashboard/CLAUDE.md` — параллельный read-only view (HTTP вместо CLI)
