# Incident: ChromaDB `link_lists.bin` 98 GB bloat

**Date:** 2026-05-04 ~03:00 (post-incident triage)
**Detected by:** Disk-usage walk (`du -sh ~/coolstep`).
**Impact:** None functional — collector + dashboard ran healthy throughout.
99 GB of 853 GB `/home` consumed by a single HNSW link-list file.

## Symptom

```
~/coolstep/data/chroma/<uuid>/link_lists.bin    98 G
~/coolstep/data/chroma/<uuid>/data_level0.bin   1.6 M
~/coolstep/data/chroma/chroma.sqlite3           5.2 M
```

6 706 vectors, 26-dim float — total useful payload < 1 MB. `link_lists.bin`
inflated by ~5 orders of magnitude.

`embeddings_queue` showed 198 ops over 40 min wall-clock window
(2026-05-03 16:12 → 16:52). Roughly 5 ops/min, well below `sync_threshold`
(default 1 000). No abnormal write pressure.

## Stack

- chromadb 1.5.8 (Python 3.14 venv)
- bundled hnswlib (no separate `hnswlib` package importable)
- single collection `frames` (`hnsw:space=cosine`, otherwise defaults)
- writer: `coolstep.adapters.storage.chroma.ChromaStore` —
  `add()` per tick, `update_metadata()` every 30 ticks for backfill labels

## Root cause

Not pinned. A fresh install of the same code reproduces normal sizes:
1 vector → 0 B link_lists, 88 vectors → 0 B link_lists.

Working hypothesis: cumulative `resize_index` storms across multiple
collector restarts (debugging session, service reloads). hnswlib's
`link_lists` reallocates on resize and never shrinks; if a corrupt or
oversized `max_elements` survives a persist, subsequent loads multiply by
`resize_factor=1.2` from that ceiling. Chromadb-bundled hnswlib at this
version does not appear to clamp the persisted ceiling against actual
element count.

This pattern has been reported upstream (chroma-core/chroma#2094 and
adjacent threads) without a definitive fix.

## Recovery

```bash
systemctl --user stop coolstep-collector coolstep-dashboard
mv ~/coolstep/data/chroma ~/coolstep/data/chroma.bak.$(date +%Y%m%d-%H%M)
systemctl --user start coolstep-collector coolstep-dashboard
```

Collector recreates the empty `frames` collection on first
`get_or_create_collection`. Embedder needs ≥ 60 frames (1 min at 1 Hz)
before vectors start appearing in chroma — that pause is normal.

`store.db` (sqlite frames table) is the source of truth and was untouched.
ML calibration window resets (was at ~3 h of 14 d before reset).

The backup directory remains for forensics — safe to delete once a fresh
session has accumulated its own labelled vectors.

## Defensive changes (this commit)

| File | Change |
|---|---|
| `coolstep/adapters/storage/chroma.py` | `dir_size_bytes()` walks the persist dir |
| `coolstep/daemon.py` | `_chroma_size_guard()` runs every 600 ticks (10 min) — `WARN` at 500 MB, `ERROR` at 5 GB |
| `coolstep/daemon.py` | `chroma_dir_bytes` exported in `ml-state.json` for dashboard tile |

No automatic rebuild — destructive ops stay manual. The watchdog only
makes the next occurrence visible in `journalctl --user -u
coolstep-collector` and on the dashboard before the disk fills.

## Expected sizes going forward

Steady state at 1 Hz with TTL 14 days (`store.FRAMES_TTL_SEC`):

| Surface | Per day | At 14 d steady state |
|---|---|---|
| `store.db` (raw_json blob heavy) | ~150–200 MB | ~2–3 GB |
| `chroma.sqlite3` | ~5–10 MB | ~70–140 MB |
| `chroma/<uuid>/data_level0.bin` | ~9 MB (86 400 × 26 × 4 B) | ~125 MB |
| `chroma/<uuid>/link_lists.bin` | sub-MB | sub-100 MB |

`store.db` is the dominant cost — `raw_json` per frame averages 2.3 KB.
If that becomes a problem, candidates:
- drop `raw_json` once `was_hot_in_30s` is backfilled (lookahead has passed)
- compress with `zstd` column extension or shrink to JSON lines
- shorten TTL from 14 d to 7 d for frames

## Open questions

- Reproduce the bloat in a stress harness (1 Hz add + 30 s update cycle for
  several restart cycles) so the watchdog thresholds can be tuned against
  observed growth curves rather than guessed.
- Pin chromadb to a known-good version once upstream fixes land.

---

## Recurrence: 2026-05-10 — auto-commits ate `.git`

**Date:** 2026-05-10 ~13:45
**Detected by:** `git pack-objects` PID 2901759 повис на 99 % CPU + 1.15 GiB RSS,
заставив `Used: 14.5 / 14.8 GiB (98 %)` mem-bar в наблюдалке. Trigger —
плановый `claw-autopush cron` (PPID chain → `git push ex44 master`).

### Symptom

```
~/coolstep/.git                                    7.6 GiB
~/coolstep/.git/objects/pack/*.pack                ~1.1 GiB (3 packs)
loose objects                                      671 (4.52 GiB)
garbage objects                                    40  (1.91 GiB)
largest blob in history (link_lists.bin from bak)  ~940 GiB nominal size
```

`git log --oneline data/chroma/** data/ml-state.json data/drift-history.jsonl`:
сплошные `auto: coolstep (... data_level0.bin, header.bin,
index_metadata.pickle ...)`, `(... chroma.sqlite3, ml-state.json ...)`. Каждые
несколько минут — новая версия 160 MB sqlite + HNSW бинарей в blob.

### Root cause

При first incident (2026-05-04) почистили **dump на диске** (`mv data/chroma →
data/chroma.bak.…`), но:

1. `data/chroma.bak.20260504-0308/` остался **tracked'ым** в git и попал в
   первый же `git add -A` от `claw-autopush`. Его `link_lists.bin` ушёл в
   pack — отсюда 940-GiB nominal blob (sparse/highly compressible на pack-stage,
   но всё равно тяжёлый при `pack-objects`).
2. `.gitignore` ловил `data/*.db`, но chromadb пишет `chroma.sqlite3` (расш.
   `.sqlite3`, **не** `.db`) + `data_level0.bin` + `header.bin` +
   `index_metadata.pickle` + `link_lists.bin` под `data/chroma/<uuid>/`.
   Никаких из этих паттернов в ignore не было.
3. `claw-autopush` (см. `~/claw/scripts/claw-autopush`) делает `git add -A`
   (либо session-touched-list если есть `CLAUDE_SESSION_ID`) и комитит без
   pre-commit checks на размер blob / runtime-path. Каждый коммит =
   очередная инкарнация sqlite/HNSW-файлов.
4. Первая incident-recovery меняла **код** (watchdog, dir_size_bytes), но не
   правила **гитовый канал утечки** — `.gitignore` + autopush guard.

### Fix (this commit)

| Where | Change |
|---|---|
| `coolstep/.gitignore` | Покрытие `data/chroma/`, `data/chroma.bak*/`, `data/*.sqlite3*`, `data/ml-state.json`, `data/drift-history.jsonl`, `data/**/*.{bin,pickle,parquet,npy,npz}` |
| `coolstep/.git` (index) | `git rm --cached -r` для 14 уже-tracked runtime-файлов (на диске остались, daemon продолжает их писать) |
| `~/claw/scripts/claw-autopush` | Pre-commit guard: regex по runtime-paths + `BIG_BLOB_MB` (default 10 MB) → unstage и skip. Логирует `GUARD: <repo> — runtime/large blob detected, unstaging:` |

### Recovery steps (для будущих recurrences)

```bash
# 1. Stop the bleeding
touch /tmp/claude-autopush-disabled       # F3-style toggle
pkill -TERM -f 'git pack-objects'         # если висит
pkill -TERM -f 'claw-autopush'

# 2. Verify .gitignore покрывает все runtime-paths
grep -E 'chroma|sqlite3|ml-state|drift-history|\.bin|\.pickle' .gitignore

# 3. Untrack из индекса (working tree остаётся)
git rm --cached -r data/chroma data/chroma.bak.* data/ml-state.json \
                   data/drift-history.jsonl 2>/dev/null

# 4. Commit ignore + untrack
git commit -m "gitignore: untrack chromadb runtime + ml-state"

# 5. (Опционально) почистить .git от мусора:
git gc --aggressive --prune=now
git repack -a -d --depth=250 --window=250

# 6. (Опционально, ТРЕБУЕТ согласования) полное удаление blob'ов из истории:
#   git filter-repo --invert-paths \
#       --path data/chroma --path data/chroma.bak.20260504-0308 \
#       --path data/ml-state.json --path data/drift-history.jsonl
#   git push --force ex44 master
#   git push --force local master
# WARNING: переписывает SHA. Координируй force-push с любыми клонами.

# 7. Re-enable autopush
rm /tmp/claude-autopush-disabled
```

### Lessons

- **`.gitignore` = first line of defense.** Расширения `.sqlite3` ≠ `.db`.
  Покрывать актуальный набор расширений + директории целиком (`data/chroma/`)
  + belt-and-suspenders patterns (`data/**/*.bin`).
- **Backup directories tracking trap.** `mv data/chroma data/chroma.bak.*` без
  одновременного добавления `data/chroma.bak*/` в `.gitignore` =
  переселение мусора в новый путь, который ловится `git add -A`.
- **Auto-commit needs a guard.** `claw-autopush` теперь блокирует коммиты с
  runtime-paths или blob > 10 MB. Override: `CLAW_AUTOPUSH_BIG_BLOB_MB=N`
  если кейс легитимный.
- **First incident's "Defensive changes" были неполными.** Watchdog в
  `daemon.py` ловит disk bloat на runtime-стороне — но не ловит, что мусор
  утекает в git. Two surfaces, two guards.
