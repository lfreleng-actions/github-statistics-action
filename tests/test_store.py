# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the SQLite store."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

MEMORY = Path(":memory:")


def _at(day: int, hour: int = 12) -> datetime:
    """A UTC timestamp on ``day`` March 2026."""
    return datetime(2026, 3, day, hour, tzinfo=UTC)


def _day(day: int) -> date:
    """``day`` March 2026."""
    return date(2026, 3, day)


def _item(repo: str = "acme/a", number: int = 1, **extra: Any) -> Any:
    """An issue or pull request record with sensible defaults."""
    from github_statistics.store import Item

    values: dict[str, Any] = {
        "repo": repo,
        "number": number,
        "node_id": f"I_{repo}_{number}",
        "kind": "pr",
        "created_at": _at(1),
        "closed_at": None,
        "merged_at": None,
        "author": "alice",
        "author_type": "User",
        "updated_at": _at(1),
    }
    values.update(extra)
    return Item(**values)


def _run(run_id: int = 1, repo: str = "acme/a", **extra: Any) -> Any:
    """A workflow run record with sensible defaults."""
    from github_statistics.store import Run

    values: dict[str, Any] = {
        "id": run_id,
        "node_id": f"WFR_{run_id}",
        "repo": repo,
        "workflow_path": ".github/workflows/ci.yaml",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "created_at": _at(2),
        "actor": "alice",
        "actor_type": "User",
        "attempt": 1,
    }
    values.update(extra)
    return Run(**values)


def _review(review_id: int, repo: str = "acme/a", number: int = 1) -> Any:
    """A pull request review record."""
    from github_statistics.store import Review

    return Review(
        id=review_id,
        node_id=f"PRR_{review_id}",
        repo=repo,
        number=number,
        reviewer="bob",
        reviewer_type="User",
        state="APPROVED",
        submitted_at=_at(3),
    )


def _store() -> Any:
    """An in-memory store."""
    from github_statistics.store import Store

    return Store(MEMORY)


@pytest.mark.xfail(strict=True, reason="store not implemented")
@pytest.mark.parametrize(
    ("owner", "expected"),
    [
        ("acme", "acme"),
        ("../x", "_x"),
        ("a/b\\c", "a_b_c"),
        ("-.-evil", "evil"),
        ("Acme Corp", "acme_corp"),
    ],
)
def test_store_path_sanitises_components(
    tmp_path: Path, owner: str, expected: str
) -> None:
    """Hostile owner names stay inside the cache directory."""
    from github_statistics.store import store_path

    path = store_path(tmp_path, "api.github.com", owner)

    assert path == tmp_path / "api.github.com" / expected / "stats.sqlite"
    assert path.resolve().is_relative_to(tmp_path.resolve())


@pytest.mark.xfail(strict=True, reason="store not implemented")
@pytest.mark.parametrize("hostile", ["..", "...", "-", ""])
def test_store_path_rejects_names_that_sanitise_to_nothing(
    tmp_path: Path, hostile: str
) -> None:
    """A component with no safe characters left is an error."""
    from github_statistics.store import store_path

    with pytest.raises(ValueError):
        store_path(tmp_path, hostile, "acme")
    with pytest.raises(ValueError):
        store_path(tmp_path, "api.github.com", hostile)


@pytest.mark.xfail(strict=True, reason="store not implemented")
def test_item_round_trip_and_enrichment() -> None:
    """Items survive a round trip and enrichment marks them read."""
    from github_statistics.store import Enrichment

    item = _item(closed_at=_at(4), merged_at=_at(4))
    with _store() as store:
        store.upsert_items([item, _item(number=2, kind="issue")])
        assert [i.number for i in store.items_needing_enrichment("acme/a", 10)] == [
            1,
            2,
        ]

        store.mark_enriched(
            [
                Enrichment(
                    repo="acme/a",
                    number=1,
                    merged_by="carol",
                    merged_by_type="User",
                    closed_by="carol",
                    closed_by_type="User",
                    read_at=_at(5),
                )
            ]
        )

        [stored] = store.items_between(_day(4), _day(4))
        assert stored.merged_by == "carol"
        assert stored.reviews_read_at == _at(5)
        assert stored.created_at == item.created_at
        assert [i.number for i in store.items_needing_enrichment("acme/a", 10)] == [2]

        store.upsert_items(
            [_item(closed_at=_at(4), merged_at=_at(4), updated_at=_at(6))]
        )
        [again, _] = store.items_needing_enrichment("acme/a", 10)
        assert again.number == 1
        assert again.merged_by == "carol"


@pytest.mark.xfail(strict=True, reason="store not implemented")
def test_items_between_filters_by_day_and_repository() -> None:
    """Day ranges are inclusive and the repository filter chunks."""
    with _store() as store:
        store.upsert_items(
            [
                _item(number=1, created_at=_at(1, 0)),
                _item(number=2, created_at=_at(2, 23)),
                _item(number=3, created_at=_at(3, 0)),
                _item(repo="acme/b", number=1, created_at=_at(2)),
            ]
        )

        assert sorted(
            (i.repo, i.number) for i in store.items_between(_day(1), _day(2))
        ) == [("acme/a", 1), ("acme/a", 2), ("acme/b", 1)]
        repos = ["acme/a", *(f"acme/x{n}" for n in range(1200))]
        assert sorted(
            i.number for i in store.items_between(_day(1), _day(2), repos=repos)
        ) == [1, 2]


@pytest.mark.xfail(strict=True, reason="store not implemented")
def test_replace_reviews_replaces_one_pull_requests_set() -> None:
    """A re-read replaces that pull request's reviews and no other's."""
    with _store() as store:
        store.replace_reviews("acme/a", 1, [_review(1), _review(2)])
        store.replace_reviews("acme/a", 2, [_review(3, number=2)])
        store.replace_reviews("acme/a", 1, [_review(2)])

        assert store.reviews_for("acme/a", 1) == [_review(2)]
        assert store.reviews_for("acme/a", 2) == [_review(3, number=2)]
        assert [r.id for r in store.reviews_between(_day(3), _day(3))] == [2, 3]


@pytest.mark.xfail(strict=True, reason="store not implemented")
def test_run_round_trip_and_job_counts() -> None:
    """Runs round trip; job counts attach and pending runs are listed."""
    pending = _run(2, status="in_progress", conclusion=None)
    with _store() as store:
        store.upsert_runs([_run(1), pending])
        assert [r.id for r in store.runs_needing_jobs(10)] == [1, 2]
        assert store.pending_runs("acme/a") == [pending]

        store.set_job_counts({1: 7})

        assert store.runs_between(_day(2), _day(2))[0] == _run(1, jobs=7)
        assert [r.id for r in store.runs_needing_jobs(10)] == [2]


@pytest.mark.xfail(strict=True, reason="store not implemented")
@pytest.mark.parametrize(
    ("change", "kept"),
    [
        ({}, True),
        ({"event": "schedule"}, True),
        ({"status": "in_progress"}, False),
        ({"conclusion": "failure"}, False),
        ({"attempt": 2}, False),
    ],
)
def test_upsert_runs_resets_jobs_on_a_state_change(
    change: dict[str, Any], kept: bool
) -> None:
    """A status, conclusion or attempt change clears the job count."""
    with _store() as store:
        store.upsert_runs([_run(1)])
        store.set_job_counts({1: 7})

        store.upsert_runs([_run(1, **change)])

        [run] = store.runs_between(_day(2), _day(2))
        assert run.jobs == (7 if kept else None)


@pytest.mark.xfail(strict=True, reason="store not implemented")
def test_replace_members_keeps_first_seen() -> None:
    """Logins already present keep ``first_seen``; gone ones are deleted."""
    from github_statistics.store import Member

    with _store() as store:
        store.replace_members("acme", ["alice", "bob"], _day(1))
        store.replace_members("other", ["zed"], _day(1))
        store.replace_members("acme", ["bob", "carol"], _day(5))

        assert store.members("acme") == [
            Member("acme", "bob", _day(1), _day(5)),
            Member("acme", "carol", _day(5), _day(5)),
        ]
        assert store.members("other") == [Member("other", "zed", _day(1), _day(1))]


@pytest.mark.xfail(strict=True, reason="store not implemented")
def test_sync_state_round_trip() -> None:
    """Sync state reads back as written, and a second write replaces it."""
    from github_statistics.store import SyncState

    first = SyncState("acme/a", "runs", watermark=_at(1), etag='W/"1"')
    second = SyncState(
        "acme/a",
        "runs",
        watermark=_at(2),
        etag=None,
        oldest=_at(1),
        last_sync_at=_at(3),
        swept_to=_at(3),
    )
    with _store() as store:
        assert store.sync_state("acme/a", "runs") is None
        store.set_sync_state(first)
        assert store.sync_state("acme/a", "runs") == first
        store.set_sync_state(second)
        assert store.sync_state("acme/a", "runs") == second


@pytest.mark.xfail(strict=True, reason="store not implemented")
@pytest.mark.parametrize(
    ("ranges", "expected"),
    [
        ([(1, 3), (4, 6)], [(1, 6)]),
        ([(1, 4), (3, 6)], [(1, 6)]),
        ([(1, 6), (2, 3)], [(1, 6)]),
        ([(2, 3), (1, 6)], [(1, 6)]),
        ([(1, 2), (5, 6)], [(1, 2), (5, 6)]),
        ([(1, 2), (5, 6), (3, 4)], [(1, 6)]),
    ],
    ids=["adjacent", "overlapping", "contained", "containing", "disjoint", "bridge"],
)
def test_add_coverage_merges_ranges(
    ranges: list[tuple[int, int]], expected: list[tuple[int, int]]
) -> None:
    """Adjacent and overlapping ranges merge; disjoint ones stay apart."""
    from github_statistics.store import DayRange

    with _store() as store:
        for start, end in ranges:
            store.add_coverage("acme/a", "issues", _day(start), _day(end))

        assert store.coverage("acme/a", "issues") == [
            DayRange(_day(start), _day(end)) for start, end in expected
        ]
        assert store.coverage("acme/a", "runs") == []
        days = {d for start, end in expected for d in range(start, end + 1)}
        for day in range(1, 8):
            assert store.covered("acme/a", "issues", _day(day)) is (day in days)


@pytest.mark.xfail(strict=True, reason="store not implemented")
def test_add_coverage_rejects_a_reversed_range() -> None:
    """A range that ends before it starts is an error."""
    with _store() as store, pytest.raises(ValueError):
        store.add_coverage("acme/a", "issues", _day(3), _day(1))


@pytest.mark.xfail(strict=True, reason="store not implemented")
def test_scope_intervals() -> None:
    """A repository is in scope from the day it enters until the day it leaves."""
    with _store() as store:
        store.enter_scope("acme/a", _day(1))
        store.enter_scope("acme/b", _day(3))
        store.enter_scope("acme/a", _day(2))
        store.leave_scope("acme/a", _day(5))
        store.enter_scope("acme/a", _day(8))

        assert store.in_scope(_day(1)) == ["acme/a"]
        assert store.in_scope(_day(3)) == ["acme/a", "acme/b"]
        assert store.in_scope(_day(5)) == ["acme/b"]
        assert store.in_scope(_day(8)) == ["acme/a", "acme/b"]

        store.leave_scope("acme/b", _day(9))
        store.enter_scope("acme/b", _day(9))
        assert store.in_scope(_day(9)) == ["acme/a", "acme/b"]


@pytest.mark.xfail(strict=True, reason="store not implemented")
def test_write_rolls_back_on_an_exception() -> None:
    """An exception inside a write leaves the previous state."""
    with _store() as store:
        store.upsert_items([_item(number=1)])
        broken = _item(number=2, created_at="not a datetime")

        with pytest.raises(TypeError):
            store.upsert_items([_item(number=3), broken])

        assert store.counts()["items"] == 1


@pytest.mark.xfail(strict=True, reason="store not implemented")
def test_schema_version_mismatch_drops_every_table(tmp_path: Path) -> None:
    """A store stamped with another version is dropped and recreated."""
    from github_statistics.store import SCHEMA_VERSION, Store

    path = tmp_path / "stats.sqlite"
    with Store(path) as store:
        store.upsert_items([_item()])
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE stray(x)")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()

    with Store(path) as store:
        assert store.schema_version() == SCHEMA_VERSION
        assert set(store.counts().values()) == {0}

    connection = sqlite3.connect(path)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    connection.close()
    assert "stray" not in tables


@pytest.mark.xfail(strict=True, reason="store not implemented")
def test_store_keeps_rows_across_reopen(tmp_path: Path) -> None:
    """A matching schema version keeps the stored rows."""
    from github_statistics.store import Store

    path = tmp_path / "deep" / "stats.sqlite"
    with Store(path) as store:
        store.upsert_items([_item()])
    with Store(path) as store:
        assert store.counts()["items"] == 1


def _fill(store: Any, repo: str, key: int) -> None:
    """Write at least one row for ``repo`` into every per-repository table."""
    from github_statistics.store import SyncState

    store.upsert_items([_item(repo=repo)])
    store.replace_reviews(repo, 1, [_review(key, repo=repo)])
    store.upsert_runs([_run(key, repo=repo)])
    store.set_sync_state(SyncState(repo, "issues", watermark=_at(1)))
    store.add_coverage(repo, "issues", _day(1), _day(2))
    store.enter_scope(repo, _day(1))


@pytest.mark.xfail(strict=True, reason="store not implemented")
def test_purge_repository_leaves_nothing_behind() -> None:
    """Every row for the repository goes; other repositories stay."""
    from github_statistics.store import TABLES

    with _store() as store:
        _fill(store, "acme/private", 1)
        _fill(store, "acme/public", 2)
        store.replace_members("acme", ["alice"], _day(1))
        before = store.counts()

        store.purge_repository("acme/private")

        connection = store.connection
        for table in TABLES:
            columns = [
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            ]
            if "repo" not in columns:
                continue
            leftover = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE repo = ?", ("acme/private",)
            ).fetchone()[0]
            assert leftover == 0, table
            kept = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE repo = ?", ("acme/public",)
            ).fetchone()[0]
            assert kept >= 1, table
        assert store.counts()["members"] == before["members"]


@pytest.mark.xfail(strict=True, reason="store not implemented")
def test_prune_drops_rows_before_the_day() -> None:
    """Pruning removes finished history before the day and keeps the rest."""
    with _store() as store:
        store.upsert_items(
            [
                _item(number=1, created_at=_at(1), closed_at=_at(2)),
                _item(number=2, created_at=_at(1)),
                _item(number=3, created_at=_at(1), closed_at=_at(6)),
            ]
        )
        store.upsert_runs([_run(1, created_at=_at(1)), _run(2, created_at=_at(6))])
        store.add_coverage("acme/a", "issues", _day(1), _day(6))
        store.add_coverage("acme/a", "runs", _day(1), _day(2))

        store.prune(_day(5))

        assert sorted(i.number for i in store.items_between(_day(1), _day(9))) == [
            2,
            3,
        ]
        assert [r.id for r in store.runs_between(_day(1), _day(9))] == [2]
        assert [(r.from_day, r.to_day) for r in store.coverage("acme/a", "issues")] == [
            (_day(5), _day(6))
        ]
        assert store.coverage("acme/a", "runs") == []
