# CLAUDE.md — `coolstep/inspect/`

> CLI для интроспекции состояния. Никогда не пишет в hardware, никогда не
> мутирует данные.

**Module version:** 0.3.0
**Last synced with master:** 2026-05-13
**Connectors:**
- ↑ package → `../CLAUDE.md`
- → reads from → `core/store.py`, `adapters/collectors/`

## Subcommands

| Command | Что делает |
|---|---|
| `coolstep adapters` | Discovery probe — видит ли система collectors |
| `coolstep tail --ticks N --period S` | N tick'ов live с прямым sample (без store) |
| `coolstep stats --since 24h` | Aggregate stats из store (frames, span, temp range, throttle) |
| `coolstep export --since 24h --format csv|json --out -` | Export shortcut columns в csv/json |

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
