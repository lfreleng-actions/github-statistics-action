# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Per-day coverage of each charted metric (design §7.3).

A day is ``complete`` when every repository in scope that day has
coverage over it, ``partial`` when some do and ``none`` when none does
or no repository is in scope. Workflow days before the owner's runs
horizon are ``none``, and a workflows day with a concluded run whose
job count is still unread is at best ``partial``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from ..collect import ISSUES_METRIC, PULL_REQUESTS_METRIC, RUNS_METRIC, runs_horizon
from ..render.model import Coverage
from ..store import DayRange, Store

PULL_REQUESTS = "pull_requests"
ISSUES = "issues"
WORKFLOWS = "workflows"
WORKFLOW_OUTCOMES = "workflow_outcomes"

# The store's coverage metric behind each charted metric.
COVERAGE_METRIC = {
    PULL_REQUESTS: PULL_REQUESTS_METRIC,
    ISSUES: ISSUES_METRIC,
    WORKFLOWS: RUNS_METRIC,
    WORKFLOW_OUTCOMES: RUNS_METRIC,
}
_RUN_METRICS = (WORKFLOWS, WORKFLOW_OUTCOMES)


def _covers(ranges: Sequence[DayRange], day: date) -> bool:
    """Whether ``day`` lies in one of ``ranges``."""
    return any(r.from_day <= day <= r.to_day for r in ranges)


def _grade(covered: int, total: int) -> Coverage:
    """``complete``, ``partial`` or ``none`` for ``covered`` of ``total`` repositories."""
    if total == 0 or covered == 0:
        return Coverage.NONE
    return Coverage.COMPLETE if covered == total else Coverage.PARTIAL


def daily_coverage(
    store: Store,
    owner: str,
    metrics: Sequence[str],
    days: Sequence[date],
    scope: Sequence[Sequence[str]],
) -> dict[str, tuple[Coverage, ...]]:
    """The coverage of each of ``metrics`` on each of ``days``.

    ``scope`` holds, for each day, the repositories in scope that day.
    """
    repos = sorted({repo for day_repos in scope for repo in day_repos})
    ranges = {
        (repo, store_metric): store.coverage(repo, store_metric)
        for repo in repos
        for store_metric in {COVERAGE_METRIC[metric] for metric in metrics}
    }
    horizon = runs_horizon(store, owner) if days else None
    missing_jobs = set(store.days_missing_jobs(days[0], days[-1])) if days else set()
    result: dict[str, tuple[Coverage, ...]] = {}
    for metric in metrics:
        store_metric = COVERAGE_METRIC[metric]
        grades: list[Coverage] = []
        for day, day_repos in zip(days, scope, strict=True):
            covered = sum(
                _covers(ranges[repo, store_metric], day) for repo in day_repos
            )
            grade = _grade(covered, len(day_repos))
            if metric in _RUN_METRICS and horizon is not None and day < horizon:
                grade = Coverage.NONE
            if (
                metric == WORKFLOWS
                and grade is Coverage.COMPLETE
                and any((repo, day) in missing_jobs for repo in day_repos)
            ):
                grade = Coverage.PARTIAL
            grades.append(grade)
        result[metric] = tuple(grades)
    return result
