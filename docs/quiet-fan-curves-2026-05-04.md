# Quiet fan curves across all profiles

**Date:** 2026-05-04
**Why:** низкий шум в приоритете — liquid metal на CPU + премиальная термопрокладка
дают тепловой запас, по которому можно жертвовать RPM, не упираясь в throttle.
**Backup перед изменением:** `data/asus-fan-curves-pre-quietify-20260504-0329.txt`

## Что сделано

Все три asusctl-профиля (Quiet / Balanced / Performance) используют одну и ту же
fan-curve. Power-сторона профилей (TDP / EPP / governor / boost) — нетронута;
дифференциация осталась только в энергетике, не в шуме.

| Fan | Точки (temp °C → PWM %) |
|---|---|
| CPU | `60→0`, `63→18`, `66→35`, `69→45`, `72→56`, `75→63`, `78→81`, `79→81` |
| GPU | `58→0`, `60→18`, `63→35`, `65→43`, `67→56`, `70→66`, `72→84`, `73→84` |

- Первая точка `0%` — на TUF A15 даёт fan-stop / минимум при низкой нагрузке;
  наблюдалось 2000–2800 RPM на хвостовом ramp-down (hw-floor).
- Плато 81% / 84% — без скачка в 100% при переходе порога. Если чип уйдёт
  выше 80°C, отрабатывает chip-thermal-throttle, а не fan-spike.

## Применение

Один shell-блок (asusctl 6.x):

```bash
CPU='60c:0%,63c:18%,66c:35%,69c:45%,72c:56%,75c:63%,78c:81%,79c:81%'
GPU='58c:0%,60c:18%,63c:35%,65c:43%,67c:56%,70c:66%,72c:84%,73c:84%'
for p in Quiet Balanced Performance; do
  asusctl fan-curve --mod-profile "$p" --fan cpu --data "$CPU"
  asusctl fan-curve --mod-profile "$p" --fan gpu --data "$GPU"
  asusctl fan-curve --mod-profile "$p" --enable-fan-curves true
done
asusctl profile set Balanced; sleep 1; asusctl profile set "$(asusctl profile get | head -1 | awk '{print $3}')"
```

`profile set` cycle нужен — asusd подхватывает curve только при переключении.

## Откат

```bash
# Восстановить разные curves каждого профиля по бэкапу
# (бэкап — в RON-формате, не в --data; править вручную или через GUI ROG)
cat ~/coolstep/data/asus-fan-curves-pre-quietify-20260504-0329.txt
```

## Проверка

```bash
# pwm raw из hwmon — должны совпадать на всех профилях
grep . /sys/devices/platform/asus-nb-wmi/hwmon/hwmon7/pwm{1,2}_auto_point*

# текущие RPM
cat /sys/devices/platform/asus-nb-wmi/hwmon/hwmon6/fan{1,2}_input
```
