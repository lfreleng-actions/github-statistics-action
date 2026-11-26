# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the store's read-only aggregate queries (design §7.3)."""

from __future__ import annotations

from dataclasses import astuple
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


def _item(number: int, repo: str = "acme/a", **extra: Any) -> Any:
    """An issue or pull request record with sensible defaults."""
    from github_statistics.store import Item

    values: dict[str, Any] = {
        "repo": repo,
        "number": number,
        "node_id": f"I_{repo}_{number}",
        "kind": "pr",
        "created_at": _at(2),
        "closed_at": None,
        "merged_at": None,
        "author": "alice",
        "author_type": "User",
        "updated_at": _at(2),
    }
    values.update(extra)
    return Item(**values)


def _run(run_id: int, repo: str = "acme/a", **extra: Any) -> Any:
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
        "jobs": 3,
    }
    values.update(extra)
    return Run(**values)


def _store() -> Any:
    """An in-memory store with ``acme/a`` in scope from 1 March."""
    from github_statistics.store import Store

    store = Store(MEMORY)
    store.enter_scope("acme/a", _day(1))
    return store


def test_item_day_counts_group_by_day_and_author() -> None:
    from github_statistics.store import ActorDayCount

    with _store() as store:
        store.upsert_items(
            [
                _item(1),
                _item(2, created_at=_at(2, 23)),
                _item(3, author="bob", created_at=_at(3), closed_at=_at(5)),
                _item(4, kind="issue"),
                _item(
                    5, created_at=_at(1, 0), author="dependabot[bot]", author_type="Bot"
                ),
            ]
        )
        opened = store.item_day_counts("pr", "created_at", _day(2), _day(9))
        closed = store.item_day_counts("pr", "closed_at", _day(1), _day(9))
    assert opened == [
        ActorDayCount(_day(2), "alice", "User", 2),
        ActorDayCount(_day(3), "bob", "User", 1),
    ]
    assert closed == [ActorDayCount(_day(5), "bob", "User", 1)]


def test_item_day_counts_reject_unknown_kind_and_column() -> None:
    with _store() as store:
        with pytest.raises(ValueError, match="kind"):
            store.item_day_counts("discussion", "created_at", _day(1), _day(2))
        with pytest.raises(ValueError, match="merged_at"):
            store.item_day_counts("pr", "merged_at", _day(1), _day(2))


def test_counts_skip_days_a_repository_was_out_of_scope() -> None:
    with _store() as store:
        store.enter_scope("acme/b", _day(3))
        store.leave_scope("acme/b", _day(5))
        store.upsert_items(
            [_item(n, repo="acme/b", created_at=_at(n)) for n in range(1, 7)]
        )
        store.upsert_runs(
            [_run(n, repo="acme/b", created_at=_at(n)) for n in range(1, 7)]
        )
        items = store.item_day_counts("pr", "created_at", _day(1), _day(9))
        runs = store.run_day_counts(_day(1), _day(9))
    assert [row.day for row in items] == [_day(3), _day(4)]
    assert [row.day for row in runs] == [_day(3), _day(4)]


def test_run_day_counts_group_scheduled_runs_without_an_actor() -> None:
    from github_statistics.store import RunDayCount

    with _store() as store:
        store.upsert_runs(
            [
                _run(1),
                _run(2, jobs=None),
                _run(3, conclusion="failure", jobs=1),
                _run(4, event="schedule", actor="maintainer", jobs=2),
                _run(5, event="schedule", actor="other", jobs=2),
            ]
        )
        rows = store.run_day_counts(_day(1), _day(9))
    assert rows == [
        RunDayCount(_day(2), None, None, "schedule", "success", 2, 4),
        RunDayCount(_day(2), "alice", "User", None, "failure", 1, 1),
        RunDayCount(_day(2), "alice", "User", None, "success", 2, 3),
    ]


def test_run_day_counts_group_dynamic_runs_without_an_actor() -> None:
    with _store() as store:
        store.upsert_runs(
            [
                _run(1, event="dynamic", actor="maintainer", jobs=1),
                _run(2, event="dynamic", actor="other", jobs=2),
                _run(3, event="schedule", jobs=4),
                _run(4, event="workflow_run", jobs=8),
            ]
        )
        rows = store.run_day_counts(_day(1), _day(9))
        actors = store.actors()
    assert [astuple(row) for row in rows] == [
        (_day(2), None, None, "dynamic", "success", 2, 3),
        (_day(2), None, None, "schedule", "success", 1, 4),
        (_day(2), "alice", "User", None, "success", 1, 8),
    ]
    assert actors == [("alice", "User")]


def test_days_missing_jobs_lists_concluded_runs_only() -> None:
    with _store() as store:
        store.upsert_runs(
            [
                _run(1, jobs=None),
                _run(
                    2,
                    jobs=None,
                    status="in_progress",
                    conclusion=None,
                    created_at=_at(3),
                ),
                _run(3, created_at=_at(4)),
            ]
        )
        assert store.days_missing_jobs(_day(1), _day(9)) == [("acme/a", _day(2))]


def test_actors_lists_every_credited_login_once() -> None:
    from github_statistics.store import Review

    with _store() as store:
        store.upsert_items(
            [
                _item(1, merged_by="carol", merged_by_type="User"),
                _item(
                    2,
                    author=None,
                    author_type=None,
                    closed_by="dave",
                    closed_by_type="User",
                ),
            ]
        )
        store.replace_reviews(
            "acme/a",
            1,
            [Review(9, "R9", "acme/a", 1, "erin", "User", "APPROVED", _at(2))],
        )
        store.upsert_runs(
            [
                _run(1, actor="ci[bot]", actor_type="Bot"),
                _run(2, event="schedule", actor="frank"),
            ]
        )
        assert store.actors() == [
            ("alice", "User"),
            ("carol", "User"),
            ("ci[bot]", "Bot"),
            ("dave", "User"),
            ("erin", "User"),
        ]


def test_member_logins_match_organisations_case_insensitively() -> None:
    from github_statistics.store import Store

    with Store(MEMORY) as store:
        store.replace_members("Acme", ["alice", "bob"], _day(1))
        store.replace_members("others", ["dave"], _day(1))
        assert store.member_logins(["ACME"]) == ["alice", "bob"]
        assert store.member_logins(["acme", "OTHERS"]) == ["alice", "bob", "dave"]
        assert store.member_logins([]) == []


def test_replace_members_reconciles_every_case_of_an_organisation() -> None:
    from github_statistics.store import Member, Store

    with Store(MEMORY) as store:
        store.replace_members("Acme", ["alice", "bob"], _day(1))
        store.replace_members("others", ["dave"], _day(1))
        store.replace_members("acme", ["bob", "carol"], _day(5))
        assert store.members("Acme") == []
        assert store.members("acme") == [
            Member("acme", "bob", _day(1), _day(5)),
            Member("acme", "carol", _day(5), _day(5)),
        ]
        assert store.member_logins(["ACME"]) == ["bob", "carol"]

        store.replace_members("ACME", [], _day(6))
        assert store.member_logins(["acme"]) == []
        assert store.member_logins(["others"]) == ["dave"]
