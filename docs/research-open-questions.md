# Research — open questions

Задача Phase R — ответить на эти вопросы до того, как писать код.


## Hardware
- [ ] CPU vendor (Intel/AMD) → MSR/RAPL доступы и доступные счётчики разные
- [ ] GPU (iGPU/dGPU/обе) → разные telemetry stacks (intel_gpu_top / amdgpu / nvidia-smi)
- [ ] Sensors: что отдаёт `sensors`, какие hwmon-устройства, есть ли EC-доступ
- [ ] Tools уже на машине: turbostat, perf, sensors, intel_gpu_top
- [ ] Fan control: pwm писать можно? нужен ли модуль ec_sys / vendor-driver?

## Software baseline
- [ ] thermald — что делает на этой машине, где конфиг, какие зоны
- [ ] auto-cpufreq / tlp / power-profiles-daemon — что уже стоит, конфликты?
- [ ] Hyprland-specific: какой источник информации о фокусе/открытых окнах (hyprctl clients, fps, какие приложения top-level)
- [ ] systemd slices (у user'а уже user/compute/build/bench): можно завязать predictor на slice-метрики

## ML side
- [ ] Класс модели: LSTM / TCN / lightweight transformer / классический xgboost на rolling features. Trade-off: latency vs accuracy
- [ ] Inference budget: целевая частота принятия решений (1Hz? 5Hz? 10Hz?)
- [ ] Training mode: онлайн (continual) vs offline (раз в неделю на собранном логе)
- [ ] Сам ML не должен жрать столько, что вызовет тот же throttle (метрика: < 1% CPU, < 50MB RAM в idle)

## Action side
- [ ] Какие безопасные «soft» рычаги, в порядке возрастания инвазивности:
    - L0: только predict + log (никакого action)
    - L1: notification user'у («через 5s ожидаю throttle, рекомендую закрыть X»)
    - L2: fan curve adjustment up (только up — никогда down ниже системного)
    - L3: cpufreq scaling_max_freq понижение на 5-10%
    - L4: intel_pstate min/max perf pct
    - L5: process scheduling (cpuset / nice / cgroups)
- [ ] L0+L1 — априори безопасно. L2+ — sandbox-first, dry-run, только под флагом
- [ ] Что НЕЛЬЗЯ трогать без подтверждения user'а — список явных запретов

## Существующие проекты (для сравнения и заимствования)
- [ ] thermald — Intel reactive
- [ ] auto-cpufreq — adaptive battery/freq
- [ ] coolercontrol — UI для fan curves
- [ ] zenpower / k10temp — AMD telemetry
- [ ] похожие ML-driven cooling проекты на github (поиск в Phase R)

## Открытые вопросы для user'а
- Конкретная модель ноута (нужно для feasibility и подбора telemetry stack)?
- Целевая боль: throttle / fan noise / battery / всё сразу?
- Готов ли отдать L2+ рычаги ML или жёстко L0+L1 первое время?
- Желаемый язык агента (Python для всего? Rust для realtime hot-path?)
