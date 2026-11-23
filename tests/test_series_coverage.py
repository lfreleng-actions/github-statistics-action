# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for per-day coverage (design §7.3)."""

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


def _run(run_id: int, repo: str = "acme/a", **extra: Any) -> Any:
    """A successful push run with one job, created 2 March."""
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
        "jobs": 1,
    }
    values.update(extra)
    return Run(**values)


def _coverage(store: Any, metric: str) -> str:
    """The coverage of ``metric`` over 1 to 10 March, one letter per day."""
    from github_statistics.config import Config, Target
    from github_statistics.config.models import Metrics, Window
    from github_statistics.series import build_series

    target = Target("acme")
    config = Config(
        targets=(target,),
        window=Window(days=10),
        metrics=Metrics(workflow_outcomes=True),
    )
    series = build_series(store, config, target, until=UNTIL)
    return "".join(str(grade)[0].upper() for grade in series.coverage[metric])


def _store() -> Any:
    """An in-memory store with ``acme/a`` and ``acme/b`` in scope from 3 March."""
    from github_statistics.store import Store

    store = Store(MEMORY)
    store.enter_scope("acme/a", _day(3))
    store.enter_scope("acme/b", _day(3))
    return store


@pytest.mark.parametrize("metric", ["pull_requests", "issues"])
def test_complete_partial_and_none(metric: str) -> None:
    with _store() as store:
        store.add_coverage("acme/a", metric, _day(1), _day(8))
        store.add_coverage("acme/b", metric, _day(6), _day(9))
        result = _coverage(store, metric)
    # 1-2: nothing in scope; 3-5: a alone; 6-8: both; 9: b alone; 10: neither.
    assert result == "NNPPPCCCPN"


def test_repository_out_of_scope_does_not_need_coverage() -> None:
    with _store() as store:
        store.leave_scope("acme/b", _day(6))
        store.add_coverage("acme/a", "issues", _day(1), _day(10))
        assert _coverage(store, "issues") == "NNPPPCCCCC"


@pytest.mark.parametrize("metric", ["workflows", "workflow_outcomes"])
def test_days_before_the_runs_horizon_are_none(metric: str) -> None:
    with _store() as store:
        for repo in ("acme/a", "acme/b"):
            store.add_coverage(repo, "runs", _day(1), _day(10))
        store.upsert_runs([_run(1, created_at=_at(5))])
        assert _coverage(store, metric) == "NNNNCCCCCC"


def test_unread_job_counts_make_a_workflows_day_partial() -> None:
    with _store() as store:
        for repo in ("acme/a", "acme/b"):
            store.add_coverage(repo, "runs", _day(1), _day(10))
        store.upsert_runs(
            [
                _run(1, created_at=_at(3)),
                _run(2, repo="acme/b", created_at=_at(6), jobs=None),
                _run(3, created_at=_at(7), jobs=None, status="queued", conclusion=None),
            ]
        )
        assert _coverage(store, "workflows") == "NNCCCPCCCC"
        assert _coverage(store, "workflow_outcomes") == "NNCCCCCCCC"
