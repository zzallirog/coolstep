"""Tests for the HNSW-backed KNN store.

Validates the public surface of `HnswStore` against the ChromaStore contract
(see ADR-022 in docs/stack-decisions.md). Skipped automatically if hnswlib
is not installed (the dep lives in the optional `ml` extras).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("hnswlib")

from coolstep.adapters.storage.hnsw import HnswStore, _id_to_ts, _ts_to_id  # noqa: E402


def _vec(*xs: float) -> list[float]:
    return list(xs)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[HnswStore]:
    s = HnswStore(persist_dir=tmp_path, dim=4, initial_capacity=128)
    assert s.discover()
    try:
        yield s
    finally:
        s.close()


def test_empty_query_returns_nothing(store: HnswStore) -> None:
    assert store.query(_vec(1, 0, 0, 0), top_k=5) == []
    assert store.query_stable(_vec(1, 0, 0, 0), top_k=5) == []
    assert store.count() == 0


def test_upsert_then_query_top_k_bounds(store: HnswStore) -> None:
    for i in range(7):
        store.add(
            1000.0 + i,
            _vec(1.0, float(i), 0.0, 0.0),
            {"was_hot_in_30s": 1, "tag": f"v{i}"},
        )
    assert store.count() == 7
    res3 = store.query(_vec(1.0, 0.0, 0.0, 0.0), top_k=3)
    assert 0 < len(res3) <= 3
    res_all = store.query(_vec(1.0, 0.0, 0.0, 0.0), top_k=20)
    assert len(res_all) == 7


def test_distance_monotonic_in_cosine(store: HnswStore) -> None:
    store.add(1.0, _vec(1, 0, 0, 0), {"was_hot_in_30s": 1})
    store.add(2.0, _vec(1, 1, 0, 0), {"was_hot_in_30s": 1})
    store.add(3.0, _vec(0, 0, 0, 1), {"was_hot_in_30s": 1})
    res = store.query(_vec(1, 0, 0, 0), top_k=3)
    dists = [r["distance"] for r in res]
    assert dists == sorted(dists)
    assert res[0]["ts"] == pytest.approx(1.0)


def test_persist_and_reload_preserves_results(tmp_path: Path) -> None:
    s1 = HnswStore(persist_dir=tmp_path, dim=4, initial_capacity=64)
    assert s1.discover()
    s1.add(100.0, _vec(1, 0, 0, 0), {"was_hot_in_30s": 1, "k": "alpha"})
    s1.add(200.0, _vec(0, 1, 0, 0), {"was_hot_in_30s": 0, "k": "beta"})
    s1.persist()
    s1.close()

    s2 = HnswStore(persist_dir=tmp_path, dim=4, initial_capacity=64)
    assert s2.discover()
    res = s2.query(_vec(1, 0, 0, 0), top_k=5, labeled_only=False)
    by_ts = {round(r["ts"], 6): r for r in res}
    assert 100.0 in by_ts
    assert by_ts[100.0]["metadata"]["k"] == "alpha"
    s2.close()


def test_labeled_only_skips_unknown(store: HnswStore) -> None:
    store.add(1.0, _vec(1, 0, 0, 0), {"was_hot_in_30s": -1})
    store.add(2.0, _vec(1, 0, 0, 0), {"was_hot_in_30s": 1})
    store.add(3.0, _vec(0, 1, 0, 0), {"was_hot_in_30s": 0})
    res = store.query(_vec(1, 0, 0, 0), top_k=5, labeled_only=True)
    tss = {round(r["ts"], 6) for r in res}
    assert 1.0 not in tss
    assert 2.0 in tss


def test_query_stable_only_returns_stable(store: HnswStore) -> None:
    store.add(1.0, _vec(1, 0, 0, 0), {"is_stable": 0, "was_hot_in_30s": 1})
    store.add(2.0, _vec(1, 0, 0, 0), {"is_stable": 1, "was_hot_in_30s": 1})
    res = store.query_stable(_vec(1, 0, 0, 0), top_k=5)
    assert {round(r["ts"], 6) for r in res} == {2.0}


def test_id_collision_replaces_in_place(store: HnswStore) -> None:
    store.add(42.0, _vec(1, 0, 0, 0), {"was_hot_in_30s": 1, "ver": "a"})
    store.add(42.0, _vec(0, 1, 0, 0), {"was_hot_in_30s": 1, "ver": "b"})
    assert store.count() == 1
    res = store.query(_vec(0, 1, 0, 0), top_k=1)
    assert res[0]["metadata"]["ver"] == "b"


def test_update_metadata_merges(store: HnswStore) -> None:
    store.add(7.0, _vec(1, 0, 0, 0), {"was_hot_in_30s": -1, "ver": "a"})
    store.update_metadata(7.0, {"was_hot_in_30s": 1, "peak_temp_after": 88.5})
    res = store.query(_vec(1, 0, 0, 0), top_k=1, labeled_only=True)
    assert len(res) == 1
    meta = res[0]["metadata"]
    assert meta["was_hot_in_30s"] == 1
    assert meta["peak_temp_after"] == pytest.approx(88.5)
    assert meta["ver"] == "a"


def test_count_labeled_and_list_helpers(store: HnswStore) -> None:
    store.add(1.0, _vec(1, 0, 0, 0), {"was_hot_in_30s": -1})
    store.add(2.0, _vec(0, 1, 0, 0), {"was_hot_in_30s": 0, "is_stable": 1})
    store.add(3.0, _vec(0, 0, 1, 0), {"was_hot_in_30s": 1})
    assert store.count() == 3
    assert store.count_labeled() == 2
    stable = store.list_stable()
    assert {round(r["ts"], 6) for r in stable} == {2.0}
    unlabeled = store.list_unlabeled(before_ts=10.0)
    assert {round(r["ts"], 6) for r in unlabeled} == {1.0}


def test_ts_id_roundtrip() -> None:
    ts = 1715600000.123456
    assert _id_to_ts(_ts_to_id(ts)) == pytest.approx(ts, abs=1e-6)


def test_ensure_capacity_grows_index_past_initial(tmp_path: Path) -> None:
    """Initial capacity is 4; we add 9 vectors. `_ensure_capacity` must call
    `resize_index` so all 9 fit. Without this path tested, a long-running
    daemon overflowing its initial cap could silently lose adds — this is
    operationally critical for the multi-week soak after every release.
    """
    store = HnswStore(persist_dir=tmp_path, dim=4, initial_capacity=4)
    assert store.discover()
    try:
        for i in range(9):
            store.add(
                100.0 + i,
                _vec(1.0, float(i), 0.0, 0.0),
                {"was_hot_in_30s": 1},
            )
        assert store.count() == 9
        # All vectors queryable after grow.
        res = store.query(_vec(1.0, 0.0, 0.0, 0.0), top_k=9, labeled_only=False)
        assert len(res) == 9
    finally:
        store.close()


def test_add_reopens_db_after_persist_dir_swap(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Simulates the `embedder_refit.refit_and_swap` orphan-fd bug.

    After the daemon's HnswStore opens meta.sqlite, `refit_and_swap` does
    `live → live.backup`, `staging → live`, then `rmtree(live.backup)` on
    the next refit. The cached sqlite3 connection still points at the now
    deleted inode. Writes start returning SQLITE_READONLY because the
    rollback journal cannot be created in the vanished parent dir.

    `add()` must detect the inode mismatch via `_ensure_db_live()`,
    reopen against the new inode, and successfully insert — without
    logging "readonly database".
    """
    live = tmp_path / "hnsw"
    store = HnswStore(persist_dir=live, dim=4, initial_capacity=16)
    assert store.discover()
    try:
        store.add(1.0, _vec(1, 0, 0, 0), {"was_hot_in_30s": 1, "k": "alpha"})
        assert store.count() == 1
        original_inode = store._db_inode
        assert original_inode is not None

        # Simulate refit_and_swap mid-state: the live dir is renamed away,
        # then replaced by a fresh empty dir at the same path. Touch a
        # placeholder meta.sqlite to mimic staging being promoted to live.
        backup = tmp_path / "hnsw.backup"
        live.rename(backup)
        live.mkdir()
        (live / "meta.sqlite").touch()
        new_inode = (live / "meta.sqlite").stat().st_ino
        assert new_inode != original_inode

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="coolstep.adapters.storage.hnsw"):
            store.add(
                2.0,
                _vec(0, 1, 0, 0),
                {"was_hot_in_30s": 1, "k": "beta"},
            )

        # No readonly warning — that's the bug we're fixing.
        readonly_msgs = [
            r.getMessage() for r in caplog.records
            if "readonly" in r.getMessage().lower()
        ]
        assert readonly_msgs == [], f"unexpected readonly warning: {readonly_msgs}"

        # Inode tracking caught up to the new file.
        assert store._db_inode == new_inode

        # Insert actually landed in the new meta.sqlite (count==1 from the
        # fresh inode, not 2 from the orphan one).
        assert store.count() == 1
        res = store.query(_vec(0, 1, 0, 0), top_k=5, labeled_only=False)
        assert {round(r["ts"], 6) for r in res} == {2.0}
    finally:
        store.close()


def test_add_reopens_db_when_meta_vanishes(tmp_path: Path) -> None:
    """If meta.sqlite is unlinked mid-life (race with rmtree), `add()`
    must reopen fresh and keep working rather than wedging on the
    orphan fd."""
    store = HnswStore(persist_dir=tmp_path, dim=4, initial_capacity=16)
    assert store.discover()
    try:
        store.add(1.0, _vec(1, 0, 0, 0), {"was_hot_in_30s": 1})
        # Yank the meta.sqlite file out from under the fd — the cached
        # connection still has it mapped, but stat() on the path will fail.
        (tmp_path / "meta.sqlite").unlink()

        store.add(2.0, _vec(0, 1, 0, 0), {"was_hot_in_30s": 1, "k": "beta"})
        # Fresh file → only the post-reopen row survives.
        assert store.count() == 1
    finally:
        store.close()


def test_discover_restores_from_backup_on_missing_live(tmp_path: Path) -> None:
    """Simulates a SIGKILL mid-rename in `refit_and_swap`: the live HNSW
    dir disappears but `hnsw.backup/` is intact. `discover()` must
    restore the backup instead of silently initialising empty (which would
    lose the entire trained KNN history).
    """
    live = tmp_path / "hnsw"
    backup = tmp_path / "hnsw.backup"

    # Seed an index in `live`, persist, then move it to `hnsw.backup` to
    # simulate the post-step-1 crash state.
    s1 = HnswStore(persist_dir=live, dim=4, initial_capacity=16)
    assert s1.discover()
    s1.add(1.0, _vec(1, 0, 0, 0), {"was_hot_in_30s": 1, "k": "alpha"})
    s1.add(2.0, _vec(0, 1, 0, 0), {"was_hot_in_30s": 1, "k": "beta"})
    s1.persist()
    s1.close()
    live.rename(backup)
    assert not live.exists()
    assert (backup / "index.bin").exists()

    # Fresh discover should see no `live` dir, find `hnsw.backup/`, and
    # copy it in before initialising. Both vectors must be queryable.
    s2 = HnswStore(persist_dir=live, dim=4, initial_capacity=16)
    assert s2.discover()
    assert s2.count() == 2
    res = s2.query(_vec(1, 0, 0, 0), top_k=5, labeled_only=False)
    s2.close()
    assert {round(r["ts"], 6) for r in res} == {1.0, 2.0}
