# Quietify-mid bump — 2026-05-07 14:14

Поверх 2026-05-04 quietify-all. Цель: тише в мид-зоне (65–76°C), где
ноут проводит большую часть времени, при одновременном расширении
peak-protection до 90°C (старый curve упирался в плато 81% на 78–79°C).

## Diff (CPU, Quiet/Balanced/Performance — все три идентичны)

| temp | old % | new % | Δ |
|---:|---:|---:|---:|
| 60 |  0 |  0 |   0 |
| 63 | 18 |  — |  -- |
| 65 |  — | 10 |   — |
| 66 | 35 |  — |  -- |
| 69 | 45 |  — |  -- |
| 70 |  — | 25 |   — |
| 72 | 56 |  — |  -- |
| 73 |  — | 40 |   — |
| 75 | 63 |  — |  -- |
| 76 |  — | 55 |   — |
| 78 | 81 |  — |  -- |
| 79 | 81 |  — |  -- |
| 80 |  — | 75 |   — |
| 85 |  — | 90 |   — |
| 90 |  — |100 |   — |

GPU аналогично, anchors сдвинуты на -2°C: 58 / 63 / 68 / 71 / 74 / 78 / 83 / 88.

## asusctl --data

- CPU: `60c:0%,65c:10%,70c:25%,73c:40%,76c:55%,80c:75%,85c:90%,90c:100%`
- GPU: `58c:0%,63c:10%,68c:25%,71c:40%,74c:55%,78c:75%,83c:90%,88c:100%`

## Why

User: тротл в его профиле использования не достижим (peak за 4 дня
calibration = 91.75°C, без throttle events). Шум на мид-нагрузке = main pain.
Calibration window остаётся на оставшиеся ~74h на этой более тихой curve —
данные ближе к реальному рабочему опыту user'а.

Старый curve имел провал в защите: после 78–79°C PWM упирался в 81%, далее
полка до hw-100% на ~90°C. Новая curve даёт прогрессивный ramp 75→90→100% на
80→85→90°C, при этом мид-зона значительно тише.

## Apply

```bash
CPU="60c:0%,65c:10%,70c:25%,73c:40%,76c:55%,80c:75%,85c:90%,90c:100%"
GPU="58c:0%,63c:10%,68c:25%,71c:40%,74c:55%,78c:75%,83c:90%,88c:100%"
for p in Quiet Balanced Performance; do
  asusctl fan-curve --mod-profile "$p" --fan cpu --data "$CPU"
  asusctl fan-curve --mod-profile "$p" --fan gpu --data "$GPU"
  asusctl fan-curve --mod-profile "$p" --enable-fan-curves true
done
asusctl profile set Quiet && sleep 1 && asusctl profile set Performance
```

## Revert

Backup: `~/coolstep/data/asus-fan-curves-pre-quietify-20260507-1414.txt`.
Восстановление через те же `asusctl fan-curve --data` со значениями оттуда.
