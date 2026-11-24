# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the daily series (design §7.3)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

MEMORY = Path(":memory:")
UNTIL = date(2026, 3, 10)
CLASSES = ("member", "external", "org_bot", "external_bot")


def _at(day: int, hour: int = 12) -> datetime:
    """A UTC timestamp on ``day`` March 2026."""
    return datetime(2026, 3, day, hour, tzinfo=UTC)


def _day(day: int) -> date:
    """``day`` March 2026."""
    return date(2026, 3, day)


def _item(number: int, repo: str = "acme/a", **extra: Any) -> Any:
    """A pull request, or with ``kind="issue"`` an issue, opened 2 March."""
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
    """A successful push run with three jobs, created 2 March."""
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


def _config(days: int = 10, **identities: tuple[str, ...]) -> Any:
    """A configuration for ``acme`` charting every metric over ``days`` days."""
    from github_statistics.config import Config, Identities, Target
    from github_statistics.config.models import Metrics, Window

    return Config(
        targets=(Target("acme"),),
        window=Window(days=days),
        metrics=Metrics(workflow_outcomes=True),
        identities=Identities(**identities),
    )


def _store() -> Any:
    """An in-memory store with ``acme/a`` in scope from 1 March and one public member."""
    from github_statistics.store import Store

    store = Store(MEMORY)
    store.enter_scope("acme/a", _day(1))
    store.replace_members("acme", ["alice"], _day(1))
    return store


def _build(store: Any, config: Any = None) -> Any:
    """The series of ``acme`` ending on `UNTIL`."""
    from github_statistics.series import build_series

    config = config or _config()
    return build_series(store, config, config.targets[0], until=UNTIL)


def test_window_is_days_ending_on_until() -> None:
    with _store() as store:
        series = _build(store)
    assert series.days == tuple(_day(n) for n in range(1, 11))
    assert series.repositories == (1,) * 10


def test_owner_of_a_user_owned_target_counts_as_a_member() -> None:
    from github_statistics.config import Config, Identities, Target
    from github_statistics.config.models import Window
    from github_statistics.series import build_series
    from github_statistics.store import Store

    target = Target("Jane", identities=Identities(member_orgs=("acme",)))
    config = Config(targets=(target,), window=Window(days=10))
    with Store(MEMORY) as store:
        store.enter_scope("Jane/a", _day(1))
        store.replace_members("acme", ["alice"], _day(1))
        store.upsert_items(
            [
                _item(1, repo="Jane/a", author="jane"),
                _item(2, repo="Jane/a"),
                _item(3, repo="Jane/a", author="bob"),
            ]
        )
        series = build_series(store, config, target, until=UNTIL)
    opened = series.values("pull_requests", "opened", "member")
    assert opened == (0, 2, 0, 0, 0, 0, 0, 0, 0, 0)
    external = series.values("pull_requests", "opened", "external")
    assert external == (0, 1, 0, 0, 0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize("kind,metric", [("pr", "pull_requests"), ("issue", "issues")])
def test_items_count_opened_and_closed_on_their_own_days(
    kind: str, metric: str
) -> None:
    with _store() as store:
        store.upsert_items(
            [
                _item(
                    1,
                    kind=kind,
                    closed_at=_at(5),
                    merged_at=_at(5) if kind == "pr" else None,
                ),
                _item(2, kind=kind, author="bob", created_at=_at(3), closed_at=_at(3)),
            ]
        )
        series = _build(store)
    assert series.values(metric, "opened", "member") == (0, 1, 0, 0, 0, 0, 0, 0, 0, 0)
    assert series.values(metric, "opened", "external") == (0, 0, 1, 0, 0, 0, 0, 0, 0, 0)
    assert series.values(metric, "closed", "member") == (0, 0, 0, 0, 1, 0, 0, 0, 0, 0)
    assert series.values(metric, "closed", "external") == (0, 0, 1, 0, 0, 0, 0, 0, 0, 0)


def test_closed_goes_to_the_author_not_the_closer() -> None:
    with _store() as store:
        store.upsert_items(
            [
                _item(
                    1,
                    closed_at=_at(3),
                    merged_at=_at(3),
                    merged_by="bob",
                    merged_by_type="User",
                )
            ]
        )
        series = _build(store)
    assert series.values("pull_requests", "closed", "member")[2] == 1
    assert series.values("pull_requests", "closed", "external")[2] == 0


def test_reopened_item_counts_as_opened_only() -> None:
    with _store() as store:
        store.upsert_items([_item(1, closed_at=_at(3)), _item(1, closed_at=None)])
        series = _build(store)
    assert sum(series.values("pull_requests", "opened", "all")) == 1
    assert sum(series.values("pull_requests", "closed", "all")) == 0


def test_workflow_runs_jobs_and_outcomes() -> None:
    with _store() as store:
        store.upsert_runs(
            [
                _run(1),
                _run(2, conclusion="failure", jobs=2),
                _run(3, conclusion="startup_failure", jobs=0),
                _run(4, conclusion="timed_out", jobs=1),
                _run(5, conclusion="cancelled", jobs=4),
                _run(6, status="in_progress", conclusion=None, jobs=None),
                _run(7, actor="dependabot[bot]", actor_type="Bot", created_at=_at(4)),
            ]
        )
        series = _build(store, _config(external_bots=("dependabot",)))
    assert series.values("workflows", "runs", "member")[1] == 6
    assert series.values("workflows", "jobs", "member")[1] == 10
    assert series.values("workflow_outcomes", "succeeded", "member")[1] == 1
    assert series.values("workflow_outcomes", "failed", "member")[1] == 3
    assert series.values("workflows", "runs", "external_bot")[3] == 1
    assert series.values("workflows", "jobs", "external_bot")[3] == 3


def test_scheduled_run_is_an_org_bot_whatever_its_actor() -> None:
    with _store() as store:
        store.upsert_runs(
            [_run(1, event="schedule"), _run(2, event="schedule", actor="ghost")]
        )
        series = _build(store, _config(ignore_logins=("alice",)))
    assert series.values("workflows", "runs", "org_bot")[1] == 2
    assert series.values("workflows", "runs", "member")[1] == 0
    assert series.values("workflow_outcomes", "succeeded", "org_bot")[1] == 2


def test_out_of_scope_days_are_not_counted() -> None:
    with _store() as store:
        store.enter_scope("acme/b", _day(4))
        store.leave_scope("acme/b", _day(6))
        store.upsert_items(
            [_item(n, repo="acme/b", created_at=_at(n)) for n in range(1, 9)]
        )
        store.upsert_runs(
            [_run(n, repo="acme/b", created_at=_at(n)) for n in range(1, 9)]
        )
        series = _build(store)
    expected = (0, 0, 0, 1, 1, 0, 0, 0, 0, 0)
    assert series.values("pull_requests", "opened", "all") == expected
    assert series.values("workflows", "runs", "all") == expected
    assert series.repositories == (1, 1, 1, 2, 2, 1, 1, 1, 1, 1)


def test_ignored_logins_are_left_out() -> None:
    with _store() as store:
        store.upsert_items([_item(1, author="Spam[bot]", author_type="Bot"), _item(2)])
        store.upsert_runs([_run(1, actor="spam", actor_type="Bot")])
        series = _build(store, _config(ignore_logins=("spam",)))
        assert series.unclassified_bots == ()
    assert sum(series.values("pull_requests", "opened", "all")) == 1
    assert sum(series.values("workflows", "runs", "all")) == 0


def test_aggregates_sum_their_classes() -> None:
    with _store() as store:
        store.upsert_items(
            [
                _item(1),
                _item(2, author="bob"),
                _item(3, author="releaser"),
                _item(4, author="newapp[bot]", author_type="Bot"),
            ]
        )
        series = _build(store, _config(org_bots=("releaser",)))
    day = 1
    assert [series.values("pull_requests", "opened", c)[day] for c in CLASSES] == [
        1,
        1,
        1,
        1,
    ]
    assert series.values("pull_requests", "opened", "humans")[day] == 2
    assert series.values("pull_requests", "opened", "bots")[day] == 2
    assert series.values("pull_requests", "opened", "all")[day] == 4
    assert series.unclassified_bots == ("newapp[bot]",)


def test_disabled_metrics_are_not_built() -> None:
    from github_statistics.config import Config, Target
    from github_statistics.config.models import Window

    config = Config(targets=(Target("acme"),), window=Window(days=10))
    with _store() as store:
        series = _build(store, config)
    assert set(series.coverage) == {"pull_requests", "issues", "workflows"}
    assert not any(key[0] == "workflow_outcomes" for key in series.counts)


_LOGINS = st.sampled_from(
    ["alice", "bob", "carol", "releaser", "dependabot[bot]", "app", None]
)
_TYPES = st.sampled_from(["User", "Bot", None])
_EVENTS = st.sampled_from(["push", "pull_request", "schedule"])
_CONCLUSIONS = st.sampled_from(["success", "failure", "cancelled", None])


_REPOS = st.sampled_from(["acme/a", "acme/b"])
_DAYS = st.integers(1, 12)


def _items(rows: list[tuple[Any, ...]]) -> list[Any]:
    """Items numbered in order from ``(repo, kind, author, type, opened, closed)``."""
    return [
        _item(
            number,
            repo=repo,
            kind=kind,
            author=author,
            author_type=author_type,
            created_at=_at(opened),
            closed_at=None if closed is None else _at(closed),
        )
        for number, (repo, kind, author, author_type, opened, closed) in enumerate(rows)
    ]


def _runs(rows: list[tuple[Any, ...]]) -> list[Any]:
    """Runs numbered in order from ``(repo, event, conclusion, actor, type, day, jobs)``."""
    return [
        _run(
            run_id,
            repo=repo,
            event=event,
            conclusion=conclusion,
            actor=actor,
            actor_type=actor_type,
            created_at=_at(day),
            jobs=jobs,
        )
        for run_id, (
            repo,
            event,
            conclusion,
            actor,
            actor_type,
            day,
            jobs,
        ) in enumerate(rows)
    ]


# Random items and runs over March 2026 in two repositories. Built from
# combinators rather than @st.composite, so type checkers without
# Hypothesis installed still see the call as complete.
_CONTENTS = st.tuples(
    st.lists(
        st.tuples(
            _REPOS,
            st.sampled_from(["pr", "issue"]),
            _LOGINS,
            _TYPES,
            _DAYS,
            st.none() | _DAYS,
        ),
        max_size=15,
    ).map(_items),
    st.lists(
        st.tuples(
            _REPOS,
            _EVENTS,
            _CONCLUSIONS,
            _LOGINS,
            _TYPES,
            _DAYS,
            st.none() | st.integers(0, 5),
        ),
        max_size=15,
    ).map(_runs),
)


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_CONTENTS)
def test_classes_sum_to_all_on_every_day(contents: tuple[list[Any], list[Any]]) -> None:
    items, runs = contents
    with _store() as store:
        store.enter_scope("acme/b", _day(3))
        store.leave_scope("acme/b", _day(8))
        store.upsert_items(items)
        store.upsert_runs(runs)
        series = _build(store, _config(org_bots=("releaser",), ignore_logins=("app",)))
    pairs = {(metric, name) for metric, name, _ in series.counts}
    for metric, name in pairs:
        total = series.values(metric, name, "all")
        by_class = [series.values(metric, name, c) for c in CLASSES]
        assert tuple(map(sum, zip(*by_class, strict=True))) == total
        humans = series.values(metric, name, "humans")
        bots = series.values(metric, name, "bots")
        assert tuple(h + b for h, b in zip(humans, bots, strict=True)) == total
