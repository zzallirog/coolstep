# CLAUDE.md — `docs/`

> Проектная документация. Не auto-generated. Каждый файл — стабильный
> long-lived артефакт.

**Module version:** 0.1.0
**Last synced with master:** 2026-05-03
**Connectors:**
- ↑ master → `../CLAUDE.md`
- ← rendered by → `dashboard/server.py:/api/stack-rationale` (для stack-decisions.md)

## Layout

```
docs/
├── concept.md                    # Что строим и почему
├── physics-rationale.md          # Arrhenius + Poole-Frenkel формальное обоснование
├── architecture.md               # Модульная схема + контракты
├── stack-decisions.md            # 15 ADR-style решений
├── workload-fingerprint.md       # Сигнатура нагрузки vs «список приложений»
├── telemetry-schema.md           # Что собирается и куда уходит
├── calibration-gates.md          # Когда predictor выходит live
├── platform-support-matrix.md    # Где работает / куда расширяемся
├── research-open-questions.md    # Что выясняли в Phase R
└── incident-YYYY-MM-DD-<slug>.md # Postmortem'ы (1-shot артефакты)
```

## Invariants

- **stack-decisions.md** парсится регэксом в dashboard. Заголовки строго
  `^## ADR-NNN: Title$`. Не менять формат без syncа с
  `dashboard/server.py:_ADR_HEADER_RE`.
- **physics-rationale.md** — только формальное обоснование. Mythology,
  spiralism, философия → не сюда.
- **architecture.md** — single source of truth для контрактов. Если код
  отошёл от описанного — фикси код, не доку.
- **Никаких generated TOC** — пиши руками. Auto-TOC рот ломает diff'ы.
- **Cross-links используют relative paths** (`../coolstep/core/CLAUDE.md`).

## Стиль

- **RU/EN свободно** в одном файле, по контексту. Заголовки чаще EN, body чаще RU.
- **Backticks для путей/команд/flag'ов**.
- **`> blockquote`** для tl;dr и cross-pointers.
- **Таблицы** для compare/summary, ASCII boxes для архитектурных схем.
- **ASCII не Unicode boxes** — лучше копи-пастится в чат.

## Visit when

- добавляешь раздел документации → этот файл (Layout) + master CLAUDE.md
  (Doc index)
- меняешь архитектуру → architecture.md + соответствующий subdir CLAUDE.md
- меняешь stack → stack-decisions.md (новый ADR) + master CLAUDE.md
- меняешь schema → telemetry-schema.md + core/store.py SCHEMA_SQL

## FAQ

**Q: Почему ADR-style вместо одной длинной "Decisions"?**
A: ADR-NNN дискретны, иммутабельны (status: accepted/superseded/deprecated),
парсятся механически, легко цитируются ("ADR-010 говорит ..."). См. ADR-007
+ ADR-008 как пример.

**Q: Куда для draft / personal notes?**
A: НЕ в docs/. В `~/.claude/work/coolstep/` — личное workspace. Сюда — только
ratified.

**Q: Numbered file naming?**
A: Нет. Тематические имена. Numbering ADR'ов внутри stack-decisions.md
достаточно.

## Sibling pointers

- → `../coolstep/core/CLAUDE.md` — что описывает architecture.md
- → `../coolstep/adapters/collectors/CLAUDE.md` — что описывает telemetry-schema.md
- → `../coolstep/dashboard/CLAUDE.md` — потребитель stack-decisions.md
- → `../tests/CLAUDE.md` — testing patterns, ссылается в стэк-decisions
