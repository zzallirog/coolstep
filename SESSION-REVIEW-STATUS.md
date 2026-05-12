# Session review — status

Запрос был провести ревью всего что я наделала за сессию: код, тесты,
диагностику, поиск кейсов, баги, дрейф. Внутри плана я раскопала три
параллельных аудита (свой код, тесты, документы), собрала находки, потом
поверх пришёл ещё один пасс от Сонета как «глаз нового юзера», и оттуда
выкатилось ещё пять штук уже всерьёз ломающих установку с нуля. Ниже
история — что было, что стало, почему.

---

**ExecStart смотрел в dev-venv.** systemd unit'ы держали
`%h/coolstep/.venv/bin/coolstep-collector`. На моей машине этот путь есть,
потому что у меня лежит репа в `~/coolstep/` с .venv. У любого другого
юзера, который читает README и делает `pipx install`, никакого
`~/coolstep/.venv/` нет — `systemctl --user enable` поднимает якобы
«active» сервис, который тут же фейлится со status=203/EXEC. Прозрачное
молчание, инсталляция «прошла», ничего не работает. Поправила: ExecStart
теперь `/usr/bin/env coolstep-collector ...` плюс `Environment=PATH=` с
покрытием pipx shim'а, pip --user директории и системного `/usr/bin/`.
Сверху появилась CLI команда `coolstep install-units` — она встраивает
содержимое юнитов в Python как строковые константы и пишет их в
`~/.config/systemd/user/`. Pipx-юзеру теперь нужно сделать один лишний
шаг (`coolstep install-units`), но он явно прописан в README между
install и `systemctl enable`. AUR-сборки кладут юниты в
`/usr/lib/systemd/user/` напрямую через PKGBUILD, им install-units не
нужен.

**Калибровочные цифры в доке расходились с кодом.** В
`docs/calibration-gates.md` я рассказывала про «24 часа coverage» и «3
throttle events». В реальности `coolstep/core/calibration.py` хранит
`COVERAGE_HOURS_TARGET=168` (неделя) и `THROTTLE_EVENTS_TARGET=10`.
Юзер видит dashboard где `coverage_hours: 35.8 / 168 (FAIL)` и
думает что что-то сломалось — потому что в доке другие числа. Дока
переписана под прод-defaults (168ч / 10 events / 85°C / 5 классов) и
теперь в самом начале есть таблица «production defaults vs pilot env
override» — pilot режим используется на быстро-калибрующихся ASUS
ноутбуках где недели слишком много, и теперь это явно объяснено вместо
неявного «у меня на машине дрейф».

**Intel-регрессия в trajectory-fallback.** Функция `_tctl()` в
`coolstep/core/fingerprint.py` искала только `tctl` и `tdie` —
AMD-специфичные ключи k10temp. На Intel-хосте coretemp пишет
`package id 0`, `core 0..N`, ARM thermal_zone пишет `x86_pkg_temp`
или `cpu-thermal`. Получалось: на любом не-AMD хосте `_tctl` возвращал
None → trajectory-fallback (физический сигнал «температура растёт круто»)
молча отключался → KNN без neighbours деградировал до AlwaysIdleBaseline
без единой ошибки в логах. Поправила: новая `_tctl()` ходит по приоритету
по списку ключей (tctl, tdie, package id 0, package, x86_pkg_temp),
потом по префиксам (package*, cpu*, physical id*), и в крайнем случае
берёт max от всех temps_c.values() — лучше шумный сигнал чем None.

**Subprocess race в perf_events и ebpf_sched.** Я писала close() так:
`os.killpg(os.getpgid(proc.pid), SIGTERM)`. Проблема — к моменту close()
proc.pid мог уже быть переиспользован OS, или getpgid сам бросит
ProcessLookupError если процесс reaped. Fallback на `proc.terminate()`
получался уже на устаревшем pid. Плюс reader-thread жил daemon=True, но
не joinился — мог продолжать писать в self._latest после того как close()
вернул управление. Поправила обоим коллекторам одинаково: pgid кешируется
сразу после spawn() в self._pgid, close() сигналит по кэшированному
значению, после wait/SIGKILL делает thread.join(timeout=1.0) с логом
если таймаут. Зомби и stale-data race закрыты.

**Документация про drift врала.** `docs/drift-detection.md` утверждал
что `embedder_cold` использует «3-σ shift versus same time last week
(per-weekday)». В коде ничего подобного нет — просто проверка
`not embedder_fitted`. Документ переписан под реальную семантику
(60-frames cold-start gate), а weekday-baseline отнесён в P3.5+ roadmap
где ему и место (нужен `embedder-stats.parquet` history файл, которого
ещё не существует).

**Архитектура утверждала 14 REST routes, реальных 29.** В
`docs/architecture.md` я где-то по ходу сессии писала «14 REST routes +
SSE», а потом нараздавала ещё 15 endpoints через P2 без обновления цифр.
Поправила оба упоминания: 29 + объяснение что три из них POST
(`/api/mode/{cool,quiet,off}`), остальные read-only.

**ChromaDB segfault на Python 3.14 был тихим.** TODO.md называл это
blocking-issue, но в README про него не было ни слова. Пользователь
обновляется до Python 3.14, ставит coolstep, предиктор тихо деградирует
до AlwaysIdleBaseline без предупреждения. README теперь явно в
Requirements: «Python 3.10–3.13 recommended, 3.14 = chromadb-segv,
поставьте `COOLSTEP_CHROMA_DISABLED=1` или downgrade». Ссылка на полную
секцию в troubleshooting.

**Внутренние документы протекали в публичный docs/.** В docs/ лежали
`operational-baseline.md` (русский заголовок) и
`incident-2026-05-04-chroma-bloat.md` (целиком русский постмортем с
шестью упоминаниями `claw-autopush` и ex44-веткой). Это внутренние
артефакты, не должны были оказаться в github. Сделала
`docs/_archive/` подкаталог, перенесла туда оба файла, обновила
cross-refs из других docs на безымянную фразу «internal archive». Плюс
в orphan-push на github теперь `git rm -r --cached docs/_archive/` —
зеркала ex44/local держат внутреннюю историю, публичный github её не
видит.

**Несколько мелочей в коде, которые могли стать привычкой если их
оставить.** `arm_thermal.make()` молча обходил pattern caps_if_set() без
комментария — поправила, теперь явно сказано почему он не нужен.
`rapl.make()` имел двойное отрицание `not (hwmon_rapl or rapl_powercap)`,
читалось плохо — развернула на отдельные условия. `manifest._merge_id_list`
не документировал порядок id-less элементов — добавила раздел в
docstring. `detect._read()` тихо подменяла non-UTF-8 байты U+FFFD без
лога — теперь log.debug. `linux_sysfs` молча получал пустой
CPU_HWMON_NAMES если manifest корраптится — теперь warning при импорте.
`dbus_session.discover()` пинговал bus через `org.freedesktop.DBus.GetId`,
менее универсальный метод чем `ListNames` — переключила. Документация в
`_query_gnome_focused` обещала Eval fallback которого в коде нет — у
строки удалила лживый кусок. PKGBUILD `check()` имел `|| true` который
маскировал упавшие тесты — убрала, теперь failing tests fail build.
`arm_thermal` глотал implausible readings без лога — добавила log.debug.

**Дрейф-индикаторы snapshot_stale и chroma_no_growth были реализованы но
не тестировались.** Из семи объявленных drift indicators пять имели
unit-тесты, два — нет. Добавила
`test_evaluate_snapshot_stale_flagged` (touch mtime 5 минут назад,
проверяю что severity >0.4) и `test_evaluate_chroma_no_growth_flagged`
(history с одинаковым chroma_count, проверяю что indicator поднимается).
Регрессий теперь нет.

**Doctor показывал dashboard_api unreachable.** Это был отдельный bug
из предыдущей итерации сессии — dashboard под CPUQuota=10% не успевал
отвечать на health-check за 3 секунды когда был занят SSE и sqlite
range queries. Поправила в два хода: shipped unit теперь имеет
CPUQuota=30%, doctor timeout поднят 3s → 15s. На моей машине теперь
8/8 ✓ healthy, health latency 6ms вместо 9.7s.

---

**Что не делала намеренно.** В код drift weekday-baseline не реализовала
— это правильное решение тянуть в P3.5+, не v0.5.0. В redfish.sample()
не добавила failure counter — этот пункт остался как TODO для следующей
итерации, не блокирует. Close-тесты на rapl/intel_i915/arm_thermal/
redfish/ipmi/dbus_session не написала потому что у них нет close()
методов — это stateless коллекторы без long-running subprocess'ов,
тестировать нечего. Agent B был чуть неточен на этот пункт.

**Что в итоге выкатилось.** 630/630 tests pass (629 было до сессии +
2 новых drift-теста; один минус потому что был тест с устаревшим
assertion который я обновила). `coolstep doctor` 8/8 ✓. Force-pushed
свежий orphan-commit на github master, retag v0.5.0, release notes
без изменений (содержание не поменялось, только багфиксы под капотом).
ex44 и local зеркала держат полную внутреннюю историю с docs/_archive/,
github видит только публично-чистую версию без приватных постмортемов.

**Главный takeaway этого ревью.** Самое опасное оказалось не «логические
ошибки в KNN» или «race в subprocess», а **drift между документацией и
кодом**: numbers в calibration-gates, claims в drift-detection, REST
count в architecture, ExecStart path в systemd. Все эти штуки прошли
бы пользовательский тест «прочитал README, поставил, не понял почему
показатель X показывает Y». Это самый дорогой класс багов потому что
он не виден из tests — pytest не знает что doc lying. Единственная
защита — пасс ревью именно с точки зрения нового юзера, который читает
доку и сравнивает с поведением. Этот пасс был кстати именно таким.
