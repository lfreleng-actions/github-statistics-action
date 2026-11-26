# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the contributor leaderboard and repository table (design §7.4)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

MEMORY = Path(":memory:")
UNTIL = date(2026, 3, 10)


def _at(day: int, hour: int = 12) -> datetime:
    """A UTC timestamp on ``day`` March 2026."""
    return datetime(2026, 3, day, hour, tzinfo=UTC)


def _day(day: int) -> date:
    """``day`` March 2026."""
    return date(2026, 3, day)


def _item(number: int, repo: str = "acme/a", **extra: Any) -> Any:
    """A pull request, or with ``kind="issue"`` an issue, by alice on 2 March."""
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


def _review(review_id: int, number: int = 1, **extra: Any) -> Any:
    """An approving review by carol of pull request ``number`` on 3 March."""
    from github_statistics.store import Review

    values: dict[str, Any] = {
        "id": review_id,
        "node_id": f"PRR_{review_id}",
        "repo": "acme/a",
        "number": number,
        "reviewer": "carol",
        "reviewer_type": "User",
        "state": "APPROVED",
        "submitted_at": _at(3),
    }
    values.update(extra)
    return Review(**values)


def _run(run_id: int, repo: str = "acme/a", **extra: Any) -> Any:
    """A push run by alice with three jobs on 2 March."""
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


def _config(top_n: int = 100, columns: tuple[str, ...] = (), **identities: Any) -> Any:
    """A ten-day configuration for ``acme``."""
    from github_statistics.config import Config, Identities, Target
    from github_statistics.config.models import Contributors, Window

    return Config(
        targets=(Target("acme"),),
        window=Window(days=10),
        contributors=Contributors(top_n=top_n, columns=columns),
        identities=Identities(**identities),
    )


def _store() -> Any:
    """An in-memory store with ``acme/a`` in scope from 1 March and alice a member."""
    from github_statistics.store import Store

    store = Store(MEMORY)
    store.enter_scope("acme/a", _day(1))
    store.replace_members("acme", ["alice"], _day(1))
    return store


def _board(store: Any, config: Any = None) -> dict[str, Any]:
    """The leaderboard of ``acme`` ending on `UNTIL`, by login."""
    from github_statistics.series import build_leaderboard

    config = config or _config()
    rows = build_leaderboard(store, config, config.targets[0], until=UNTIL)
    return {row.login: row for row in rows}


def _table(store: Any, config: Any = None) -> dict[str, Any]:
    """The repository table of ``acme`` ending on `UNTIL`, by repository."""
    from github_statistics.series import build_repositories

    config = config or _config()
    rows = build_repositories(store, config, config.targets[0], until=UNTIL)
    return {row.repo: row for row in rows}


# Aggregates


@pytest.mark.parametrize(
    "arguments",
    [
        {"kind": "commit", "column": "created_at"},
        {"kind": "pr", "column": "updated_at"},
        {"kind": "pr", "column": "created_at", "actor": "reviewer"},
    ],
)
def test_item_actor_counts_rejects_unknown_arguments(arguments: dict[str, Any]) -> None:
    with _store() as store:
        kind, column = arguments.pop("kind"), arguments.pop("column")
        with pytest.raises(ValueError):
            store.item_actor_counts(kind, column, _day(1), _day(10), **arguments)


def test_item_actor_counts_groups_by_the_chosen_actor() -> None:
    with _store() as store:
        store.upsert_items(
            [
                _item(1, merged_at=_at(3), closed_at=_at(3), merged_by="bob"),
                _item(2, closed_at=_at(4)),
            ]
        )
        merged = store.item_actor_counts(
            "pr", "merged_at", _day(1), _day(10), actor="merged_by"
        )
        unmerged = store.item_actor_counts(
            "pr", "closed_at", _day(1), _day(10), unmerged=True
        )
    assert merged == [("bob", None, 1)]
    assert unmerged == [("alice", "User", 1)]


def test_review_actor_counts_counts_states_and_distinct_pull_requests() -> None:
    with _store() as store:
        store.upsert_items([_item(1), _item(2)])
        store.replace_reviews(
            "acme/a",
            1,
            [_review(1), _review(2, state="CHANGES_REQUESTED")],
        )
        store.replace_reviews("acme/a", 2, [_review(3, number=2, state="DISMISSED")])
        rows = store.review_actor_counts(_day(1), _day(10))
        pulls = store.reviewed_pull_requests(_day(1), _day(10))
    assert rows == [("carol", "User", 3, 1, 1)]
    assert pulls == [("carol", "User", "acme/a", 1), ("carol", "User", "acme/a", 2)]


def test_run_repository_counts_credit_a_scheduled_run_to_no_login() -> None:
    with _store() as store:
        store.upsert_runs([_run(1), _run(2, event="schedule")])
        rows = store.run_repository_counts(_day(1), _day(10))
        actors = store.run_actor_counts(_day(1), _day(10))
    assert rows == [
        ("acme/a", None, None, True, 1, 3),
        ("acme/a", "alice", "User", False, 1, 3),
    ]
    assert actors == [("alice", "User", 1)]


@pytest.mark.xfail(strict=True)
def test_run_repository_counts_credit_a_dynamic_run_to_no_login() -> None:
    with _store() as store:
        store.upsert_runs([_run(1), _run(2, event="dynamic")])
        rows = store.run_repository_counts(_day(1), _day(10))
        actors = store.run_actor_counts(_day(1), _day(10))
    assert rows == [
        ("acme/a", None, None, "dynamic", 1, 3),
        ("acme/a", "alice", "User", None, 1, 3),
    ]
    assert actors == [("alice", "User", 1)]


def test_item_repository_counts_group_by_repository_and_author() -> None:
    with _store() as store:
        store.enter_scope("acme/b", _day(1))
        store.upsert_items([_item(1), _item(2), _item(1, repo="acme/b", author="bob")])
        rows = store.item_repository_counts("pr", "created_at", _day(1), _day(10))
    assert rows == [("acme/a", "alice", "User", 2), ("acme/b", "bob", "User", 1)]


# Leaderboard


def test_merged_pull_request_is_credited_to_its_author_not_the_merger() -> None:
    with _store() as store:
        store.upsert_items(
            [_item(1, merged_at=_at(3), closed_at=_at(3), merged_by="bob")]
        )
        board = _board(store)
    assert set(board) == {"alice"}
    counts = board["alice"].counts
    assert counts["prs_opened"] == 1
    assert counts["prs_merged"] == 1
    assert counts["prs_closed_unmerged"] == 0
    assert board["alice"].actor_class == "member"


def test_authored_columns_count_on_their_own_days() -> None:
    with _store() as store:
        store.upsert_items(
            [
                _item(1, closed_at=_at(4)),
                _item(2, created_at=_at(1, 0), merged_at=_at(5), closed_at=_at(5)),
                _item(3, kind="issue", closed_at=_at(6)),
                _item(4, kind="issue"),
            ]
        )
        counts = _board(store)["alice"].counts
    assert counts["prs_opened"] == 2
    assert counts["prs_merged"] == 1
    assert counts["prs_closed_unmerged"] == 1
    assert counts["issues_opened"] == 2
    assert counts["issues_closed"] == 1


def test_review_columns_count_a_self_review_and_a_dismissed_review() -> None:
    with _store() as store:
        store.upsert_items([_item(1), _item(2)])
        store.replace_reviews(
            "acme/a",
            1,
            [
                _review(1),
                _review(2, state="CHANGES_REQUESTED"),
                _review(3, reviewer="alice", state="COMMENTED"),
            ],
        )
        store.replace_reviews("acme/a", 2, [_review(4, number=2, state="DISMISSED")])
        board = _board(store)
    carol = board["carol"].counts
    assert (
        carol["reviews"],
        carol["prs_reviewed"],
        carol["approvals"],
        carol["changes_requested"],
    ) == (3, 2, 1, 1)
    assert board["alice"].counts["reviews"] == 1


def test_scheduled_runs_are_credited_to_no_login() -> None:
    with _store() as store:
        store.upsert_runs([_run(1), _run(2, event="schedule"), _run(3, actor="bob")])
        board = _board(store)
    assert board["alice"].counts["runs_triggered"] == 1
    assert board["bob"].counts["runs_triggered"] == 1


@pytest.mark.xfail(strict=True)
def test_dynamic_runs_are_credited_to_no_login() -> None:
    with _store() as store:
        store.upsert_runs([_run(1), _run(2, event="dynamic"), _run(3, actor="bob")])
        board = _board(store)
    assert board["alice"].counts["runs_triggered"] == 1
    assert board["bob"].counts["runs_triggered"] == 1


def test_both_forms_of_a_bot_login_share_one_row_with_the_suffix() -> None:
    with _store() as store:
        store.upsert_items([_item(1, author="dependabot[bot]", author_type="Bot")])
        store.replace_reviews(
            "acme/a",
            1,
            [
                _review(1, reviewer="dependabot", reviewer_type="Bot"),
                _review(2, reviewer="triage-bot", reviewer_type="Bot"),
            ],
        )
        config = _config(org_bots=("triage-bot[bot]",))
        board = _board(store, config)
    assert set(board) == {"dependabot[bot]", "triage-bot[bot]"}
    assert board["dependabot[bot]"].counts["prs_opened"] == 1
    assert board["dependabot[bot]"].counts["reviews"] == 1
    assert board["dependabot[bot]"].actor_class == "external_bot"
    assert board["triage-bot[bot]"].actor_class == "org_bot"


def test_both_forms_of_a_bot_login_count_one_pull_request_reviewed() -> None:
    with _store() as store:
        store.upsert_items([_item(1)])
        store.replace_reviews(
            "acme/a",
            1,
            [
                _review(1, reviewer="dependabot", reviewer_type="Bot"),
                _review(2, reviewer="dependabot[bot]", reviewer_type="Bot"),
            ],
        )
        counts = _board(store)["dependabot[bot]"].counts
    assert (counts["reviews"], counts["prs_reviewed"], counts["total"]) == (2, 1, 5)


def test_a_bot_seen_only_through_graphql_is_shown_with_the_suffix() -> None:
    with _store() as store:
        store.upsert_items([_item(1)])
        store.replace_reviews(
            "acme/a",
            1,
            [
                _review(1, reviewer="dependabot", reviewer_type="Bot"),
                _review(2, reviewer="ci-bot", reviewer_type="User"),
            ],
        )
        board = _board(store, _config(org_bots=("ci-bot",)))
    assert set(board) == {"alice", "dependabot[bot]", "ci-bot"}
    assert board["ci-bot"].actor_class == "org_bot"


def test_optional_columns_are_off_by_default_and_enabled_by_name() -> None:
    with _store() as store:
        store.upsert_items(
            [
                _item(1, merged_at=_at(3), closed_at=_at(3), merged_by="bob"),
                _item(2, kind="issue", closed_at=_at(4), closed_by="bob"),
            ]
        )
        plain = _board(store)
        extended = _board(store, _config(columns=("merged_by", "closed_by")))
    assert "merged_as_merger" not in plain["alice"].counts
    assert "bob" not in plain
    bob = extended["bob"].counts
    assert bob["merged_as_merger"] == 1
    assert bob["closed_as_closer"] == 1
    assert bob["total"] == 0


def test_total_sums_every_default_column_and_sorts_rows() -> None:
    with _store() as store:
        store.upsert_items(
            [
                _item(1, merged_at=_at(3), closed_at=_at(3)),
                _item(2, author="zed"),
                _item(3, author="bob"),
            ]
        )
        store.replace_reviews("acme/a", 2, [_review(1, number=2)])
        store.upsert_runs([_run(1, actor="zed")])
        from github_statistics.series import build_leaderboard

        config = _config()
        rows = build_leaderboard(store, config, config.targets[0], until=UNTIL)
    assert [(row.login, row.counts["total"]) for row in rows] == [
        ("carol", 3),
        ("alice", 2),
        ("zed", 2),
        ("bob", 1),
    ]


@pytest.mark.parametrize(("top_n", "expected"), [(2, 2), (0, 3)])
def test_top_n_caps_the_rows(top_n: int, expected: int) -> None:
    with _store() as store:
        store.upsert_items([_item(1), _item(2, author="bob"), _item(3, author="zed")])
        board = _board(store, _config(top_n=top_n))
    assert len(board) == expected


def test_ignored_logins_and_out_of_scope_events_are_left_out() -> None:
    with _store() as store:
        store.enter_scope("acme/b", _day(5))
        store.upsert_items(
            [
                _item(1, author="ghost-writer"),
                _item(2, repo="acme/b", author="bob"),
                _item(3, repo="acme/c", author="bob"),
            ]
        )
        board = _board(store, _config(ignore_logins=("Ghost-Writer",)))
    assert board == {}


# Repository table


def test_repository_table_counts_per_repository() -> None:
    with _store() as store:
        store.enter_scope("acme/b", _day(1))
        store.upsert_items(
            [
                _item(1, closed_at=_at(4)),
                _item(2, kind="issue", closed_at=_at(4)),
                _item(3, repo="acme/b"),
                _item(4, repo="acme/b", author="ignored"),
            ]
        )
        store.upsert_runs([_run(1), _run(2, event="schedule", actor="ignored")])
        table = _table(store, _config(ignore_logins=("ignored",)))
    assert sorted(table) == ["acme/a", "acme/b"]
    assert dict(table["acme/a"].counts) == {
        "prs_opened": 1,
        "prs_closed": 1,
        "issues_opened": 1,
        "issues_closed": 1,
        "runs": 2,
        "jobs": 6,
    }
    assert table["acme/b"].counts["prs_opened"] == 1


@pytest.mark.xfail(strict=True)
def test_repository_table_counts_a_dynamic_run_whatever_its_actor() -> None:
    with _store() as store:
        store.upsert_runs([_run(1, event="dynamic", actor="ignored")])
        table = _table(store, _config(ignore_logins=("ignored",)))
    assert table["acme/a"].counts["runs"] == 1
    assert table["acme/a"].counts["jobs"] == 3


def test_repository_table_reports_the_share_of_days_covered() -> None:
    from github_statistics.collect import (
        ISSUES_METRIC,
        PULL_REQUESTS_METRIC,
        RUNS_METRIC,
    )

    with _store() as store:
        store.add_coverage("acme/a", PULL_REQUESTS_METRIC, _day(1), _day(5))
        store.add_coverage("acme/a", ISSUES_METRIC, _day(1), _day(10))
        store.add_coverage("acme/a", RUNS_METRIC, _day(1), _day(10))
        coverage = _table(store)["acme/a"].coverage
    assert dict(coverage) == {"pull_requests": 0.5, "issues": 1.0, "workflows": 1.0}


def test_repository_table_lists_only_repositories_in_scope() -> None:
    with _store() as store:
        store.enter_scope("acme/b", _day(1))
        store.leave_scope("acme/b", _day(1))
        store.upsert_items([_item(1, repo="acme/c")])
        table = _table(store)
    assert list(table) == ["acme/a"]


def test_repository_table_reads_window_wide_coverage_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from github_statistics.collect import RUNS_METRIC
    from github_statistics.series.coverage import daily_coverage
    from github_statistics.series.daily import window

    with _store() as store:
        store.enter_scope("acme/b", _day(1))
        for repo in ("acme/a", "acme/b"):
            store.add_coverage(repo, RUNS_METRIC, _day(1), _day(10))
        store.upsert_runs(
            [
                _run(1, created_at=_at(4)),
                _run(2, repo="acme/b", created_at=_at(6), jobs=None),
            ]
        )
        days = window(10, UNTIL)
        expected = {
            repo: daily_coverage(store, "acme", ["workflows"], days, [[repo]] * 10)
            for repo in ("acme/a", "acme/b")
        }
        calls: list[str] = []
        for name in ("earliest_run_day", "days_missing_jobs"):
            method = getattr(store, name)

            def counted(*args: Any, _name: str = name, _method: Any = method) -> Any:
                calls.append(_name)
                return _method(*args)

            monkeypatch.setattr(store, name, counted)
        table = _table(store)
    assert sorted(calls) == ["days_missing_jobs", "earliest_run_day"]
    for repo, grades in expected.items():
        complete = sum(grade.value == "complete" for grade in grades["workflows"])
        assert table[repo].coverage["workflows"] == complete / 10
    assert table["acme/a"].coverage["workflows"] == 0.7
    assert table["acme/b"].coverage["workflows"] == 0.6
