# Operational baseline для научных тестов

> Hardware setup target machine: liquid metal CPU, master pad job, идеальный
> thermal interface на всех чипах. Это даёт право работать в режиме «более
> жёстко, более тихо», но coolstep собирает данные для генерации продукта на
> ноуты с **худшим теплообменом**, поэтому baseline должен быть штатным —
> воспроизводимым на любом чипе из этой серии.

## Изменённые курвы (2026-05-03)

User'ская **Quiet** курва была экспоненциальная (5-22-38-45-56-63-81-81 на
60-78°C — долго тихо, потом резкий ramp). Это удобно в быту, но создаёт
**нелинейный baseline**, на который ML обучается с искажением.

Заменена на линейный ramp под scientific tests:

```
asusctl fan-curve --mod-profile Quiet --fan cpu \
    --data '30c:5%,45c:15%,55c:25%,65c:40%,72c:55%,78c:70%,84c:85%,90c:100%'

asusctl fan-curve --mod-profile Quiet --fan gpu \
    --data '30c:0%,45c:10%,55c:20%,65c:35%,72c:55%,78c:70%,84c:85%,90c:100%'

asusctl fan-curve --mod-profile Quiet --enable-fan-curves true
```

Backup оригинальной curve лежит в `~/coolstep/data/asus-quiet-curve-2026-05-03.bak.txt`.

## Целевой характер baseline

- **idle (≤45°C)** — fan тихий (5-15% PWM), не мешает эффективному рассеянию
- **light (45-65°C)** — линейный ramp (15-40% PWM), без quiet ступеней
- **moderate (65-78°C)** — ramp 40-70%, держим headroom для пиков
- **heavy (78-90°C)** — близко к 100%, защита от throttle

Это даёт ML predictor'у чёткий signal: **fan_rpm растёт вместе с T_chip
монотонно** — модель видит зависимость и может предсказывать прогрев,
не запутавшись в quiet-плато.

## Profile policy

| Mode | Profile | Когда |
|---|---|---|
| Default | Quiet (с custom scientific curve) | Daily use, baseline data collection |
| Game | Performance (с user-tuned aggressive curve) | game-mode.service active — отдельный feature ADR-015 P2.5+ |
| Heavy build | Balanced или Performance — TBD | Когда user явно запросит |

## Game-mode integration (P2.5)

См. ADR-015 в stack-decisions.md. Когда `game-mode.service` active,
coolstep должен:

1. **Не вмешиваться** в существующую конфигурацию game-mode (asusctl
   fan-curve --mod-profile Performance + ryzenadj + nvidia-smi)
2. **Дополнительно** оптимизировать sustained boost: держать T_chip близко
   к sweet spot (см. `efficiency-curve.md`), не ниже. Это даёт
   maximum perf/W при minimum fan RPM выше необходимого.
3. **Логировать** event'ы для post-game замера — что было до/после game-mode

В P0+P1 это только observe — coolstep видит, что game-mode active (через
systemctl is-active probe), помечает frames соответствующим
workload_label, но не действует.

## Privacy

Все эти настройки локальные. Backup curve хранится в `data/` который
gitignored.

## Когда вернуть оригинал

```bash
asusctl fan-curve --mod-profile Quiet --fan cpu \
    --data '60c:5%,63c:22%,66c:38%,69c:45%,72c:56%,75c:63%,78c:81%,78c:81%'
asusctl fan-curve --mod-profile Quiet --fan gpu \
    --data '58c:5%,60c:20%,63c:38%,65c:43%,67c:56%,70c:66%,72c:84%,72c:84%'
asusctl fan-curve --mod-profile Quiet --enable-fan-curves false
```

(Это оригинал из backup — `--enable-fan-curves false` восстанавливает default.)

## Reference

- ADR-014 — efficiency proxy (work_per_degree)
- ADR-015 — game-mode optimization stub (P2.5)
- physics-rationale.md — почему линейный baseline даёт чище ML training
