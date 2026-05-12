# CLAUDE.md — `tests/`

> pytest. 98 tests на момент P0. Структура отражает coolstep/ один-в-один.

**Module version:** 0.1.0
**Last synced with master:** 2026-05-03
**Connectors:**
- ↑ master → `../CLAUDE.md`
- → tests → весь `coolstep/` package

## Layout

```
tests/
├── test_schema.py           # core/schema.py
├── test_ring.py             # core/ring.py
├── test_store.py            # core/store.py
├── test_fingerprint.py      # core/fingerprint.py
├── test_predictor.py        # core/predictor.py
├── test_decision.py         # core/decision.py
├── test_calibration.py      # core/calibration.py
├── test_daemon.py           # daemon.py end-to-end smoke
├── adapters/
│   ├── test_registry.py
│   ├── test_linux_sysfs.py  # fakeroot tmp_path
│   ├── test_nvidia_nvml.py  # mock pynvml
│   ├── test_amdgpu.py       # fakeroot tmp_path
│   ├── test_hyprctl.py      # mock subprocess
│   └── test_readonly_actuator.py
└── dashboard/
    └── test_routes.py        # FastAPI TestClient
```

## Invariants

- **Test names match module names.** test_X.py для coolstep/X.py.
- **No real hardware in tests.** Adapter тесты используют tmp_path fakeroot
  (linux_sysfs, amdgpu) или mock subprocess/SDK (nvidia_nvml, hyprctl).
- **No real network in tests.** Dashboard через TestClient (httpx-based).
- **Fixtures сверху файла, тесты ниже.** Не в conftest.py если не shared
  между файлами.
- **Один test = один assert per logical statement.** Можно несколько `assert`
  в test'е, но они проверяют одно явление с разных углов.
- **Coverage цель — гонка не самоцель**. Важнее crit-path:
  - core/schema.merge_partial: 100% (foundation)
  - core/store: 90%+ (rotation edge cases)
  - adapters: discover() + sample() основные пути; cost() smoke

## Visit when

- пишешь новый модуль → создай `test_<module>.py` параллельно
- падает CI на регрессии → добавь test, не убирай флаг lint
- добавляешь Lit components → дополнительная папка `tests/dashboard/components/`
  для DOM-тестов (P0 polish, ещё нет)

## FAQ

**Q: Где fixture для realistic TelemetryFrame?**
A: Локально в test'е через factory (`def _frame(ts, *, tctl=70.0): ...`).
Вынесём в conftest.py если 3+ файла используют.

**Q: pytest -k pattern?**
A: Стандартный. Например `pytest -k "calibration or decision"`.

**Q: Tests can write to /sys ?**
A: НИКОГДА. Только `tmp_path` fakeroot.

**Q: Slow tests?**
A: P0 нет. Все < 100ms. Если появится — `@pytest.mark.slow` + filter в CI.

**Q: Hypothesis / property-based?**
A: P0 нет. Может быть в P1+ для merge_partial / fingerprint extract.

## Sibling pointers

- → `../coolstep/core/CLAUDE.md` — что тестируется
- → `../coolstep/adapters/CLAUDE.md` — adapter test patterns
- → `../coolstep/dashboard/CLAUDE.md` — TestClient patterns
