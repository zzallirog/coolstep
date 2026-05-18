# Workload fingerprint

> Сигнатура нагрузки — это не то, **что** запущено, а **как** оно нагружает
> железо. Метафора Shazam: не «какая песня», а «какая частотная сигнатура».

## Зачем не «список приложений»

«Список открытых приложений» работает на ноуте, где user узнаёт окна. На
сервере окон нет. На Wine/Proton одна и та же игра выглядит по-разному, чем
её нативный аналог. Новое приложение, которого не было в trainset, — внеaler
классификатора.

Fingerprint фиксирует **поведение**, а не имя. Build-проект на 16 потоках и
рендер видео имеют разные ритмы пиков, даже если средний CPU% одинаковый. Это
и нужно ловить.

## Что входит в fingerprint в P0

P0-feature set (см. `coolstep/core/fingerprint.py`):

| Feature | Источник | Описание |
|---|---|---|
| `cpu_load_max` | per-core /proc/stat | Пиковый avg-load в окне |
| `cpu_load_avg` | per-core | Средний load |
| `cpu_load_p95` | per-core | 95-perc — детектит spiky workloads |
| `peak_count` | derived | Сколько ticks с load > 80% |
| `spike_density` | derived | peak_count / window_seconds |
| `cpu_temp_max` | k10temp | Пиковая Tctl в окне |
| `cpu_temp_avg` | k10temp | Средняя Tctl |
| `cpu_temp_slope_per_sec` | OLS-fit Tctl vs ts | Скорость разогрева — критический сигнал |
| `gpu_temp_max` | amdgpu+nvidia | Макс-температура любого GPU |
| `gpu_temp_avg` | то же | Средняя |
| `fan_rpm_max` | asus | Макс-RPM любого fan |
| `fan_rpm_avg` | то же | Средняя |
| `visible_apps` | hyprctl | Кол-во mapped+visible окон |
| `unique_classes` | hyprctl | Уникальных application classes |

Это **rolling features** — окно последних N tick'ов. Дешёвые, plain Python,
без numpy.

## Что добавится в P1

- **Performance counters** через `perf` events (cycles, instructions, IPC,
  cache-miss rate, branch-misprediction rate). Это ловит build vs ml-train
  vs game с большим разрешением — у каждого свой профиль cache-locality.
- **Per-process gpu_pct** через `nvidia-smi pmon` / amdgpu fdinfo. Сейчас
  hyprctl даёт только class+pid; добавим CPU% (psutil) и GPU% (smi).
- **Spectral features**: FFT по rolling window load — часто-периодические
  паттерны (game loop 60 Hz, build batch 5 Hz, video encoding 24/30 Hz).

## Cluster labelling (P1)

Unsupervised: HDBSCAN на feature vectors → кластеры с автоматическим K. Каждому
кластеру присваивается ярлык по post-hoc inspection (через dashboard «Workload
fingerprint scope» tile).

P0 кластера нет — `WorkloadFrame.label` остаётся None. Это ок, calibration
gate `class_diversity` не пройдёт пока labelling не появится → predictions не
выйдут в live.

## Cross-platform mapping

| Сигнал | Linux | Windows | macOS | Server |
|---|---|---|---|---|
| Per-core load | /proc/stat | psutil | psutil | psutil/IPMI |
| Open windows | hyprctl / wmctrl | EnumWindows | NSRunningApps | n/a |
| GPU per-proc | nvidia-smi pmon, amdgpu fdinfo | NVAPI | IOKit | по vendor |
| perf counters | linux perf | ETW | Instruments / dtrace | linux perf |

`visible_apps`/`unique_classes` на сервере уйдут в `cgroup_count` /
`active_units` — концепция та же, реализация другая. Adapter справляется
сам, ядро не меняется.

## Privacy rules

- Имя процесса (`name`) — приватная информация, в экспорт без `--include-raw`
  не уходит
- pid и количество окон — приватность мягче, идут в shortcut столбцы
- Перенос между машинами модели — **запрещён** в P0+. Каждая машина учится
  на собственных fingerprints; transfer learning рассматривается в P7+ как
  optional.
