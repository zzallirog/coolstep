# Upgrade

> Single-host coolstep, no shared state. Every upgrade is a local
> rebuild + daemon restart. State in `~/coolstep/data/`
> (`COOLSTEP_HOME`) survives the upgrade on a best-effort basis: what
> is versioned migrates; what isn't gets refit from the in-memory ring
> window and `store.db`.

## Commands by install path

**pipx (Path A):**

```bash
pipx upgrade coolstep
systemctl --user restart coolstep-collector coolstep-dashboard
```

**pip --user (Path B):**

```bash
pip install --user --break-system-packages -U git+https://github.com/zzallirog/coolstep
systemctl --user restart coolstep-collector coolstep-dashboard
```

`systemctl restart` is mandatory — the installed package is new, but
the in-memory daemon stays on the old code until the unit is restarted.

## State-file contract

| File | Survives upgrade? | What happens on a schema break |
|---|---|---|
| `store.db` (frames / throttle_events / actions / meta) | yes | sqlite `CREATE TABLE IF NOT EXISTS` adds new columns safely; column rename / type change needs a manual `ALTER`. Existing rows preserved. |
| `embedder-stats.json` | dimension-versioned | Auto-rejected on dim mismatch (e.g. 26 → 29 in v0.5.15); a clean refit + full hnsw reindex starts without user action. See v0.5.15 release notes. |
| `ml-state.json` | best-effort | No version field. On a format break the daemon rewrites the file from the ring window. |
| `runtime-state.json` | ephemeral | Holds armed_actions; restart = clean restore from `actuator-journal.jsonl`. |
| `actuator-journal.jsonl` | append-only | Stable schema since v0.5.0. Read consumers tolerate missing keys. |
| `drift-history.jsonl` | append-only | Stable schema. |
| `data/chroma/` (HNSW index) | rebuildable | Embedder dim change → full reindex from `store.db` (`reindex_hnsw_from_store.py`). ~120 MB ≈ a 2-minute rebuild on a target box. |

## Post-upgrade health check

```bash
coolstep --version          # confirm the binary actually moved
systemctl --user status coolstep-collector coolstep-dashboard
coolstep doctor             # full health check
journalctl --user -u coolstep-collector --since=-2m | grep -iE "embedder|refit|reindex"
```

Embedder log lines like `dim mismatch`, `refits frozen`, or
`loaded persisted stats` are expected after a schema-aware version bump.

## Known schema breaks

| From → To | What gets rebuilt | User action |
|---|---|---|
| 0.5.14 → 0.5.15 | embedder dim 26 → 29 (P2.11 trajectory features). Auto-reject `embedder-stats.json` + full hnsw reindex. | none — wait for `reindex complete` in the journal (~2 min). |

When rolling back to an older minor (`pipx install --force coolstep==X.Y.Z`):
- The embedder refits at the older dim → new vectors land on the old
  schema → KNN quality degrades until the ring window has rotated
  through enough fresh frames.
- Columns added in newer versions sit unused — read-safe.
