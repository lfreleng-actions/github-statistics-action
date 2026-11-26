# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The repository table: counts and coverage per repository (design §7.4).

One row per repository in scope on any day of the window, with the
counts the charts draw and, per charted metric, the share of window
days the repository covers, so a reader can see which repository
drives a spike or a gap. Counts follow the series rules: ignored logins
are left out and a scheduled or dynamic run counts whatever its actor.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

from ..collect import runs_horizon
from ..config import Config, ResolvedIdentities, Target, resolve_identities
from ..store import DayRange, Store
from .actors import ActorClass, classify, public_members
from .coverage import COVERAGE_METRIC, WORKFLOW_OUTCOMES, WORKFLOWS, _covers
from .daily import _enabled, window

COLUMNS: tuple[str, ...] = (
    "prs_opened",
    "prs_closed",
    "issues_opened",
    "issues_closed",
    "runs",
    "jobs",
)
_ITEM_QUERIES = {
    "prs_opened": ("pr", "created_at"),
    "prs_closed": ("pr", "closed_at"),
    "issues_opened": ("issue", "created_at"),
    "issues_closed": ("issue", "closed_at"),
}
# Shares are rounded so the export stays short and stable.
_SHARE_DIGITS = 4
_RUN_METRICS = (WORKFLOWS, WORKFLOW_OUTCOMES)


def _complete(
    metric: str,
    repo: str,
    day: date,
    ranges: Sequence[DayRange],
    horizon: date | None,
    missing_jobs: set[tuple[str, date]],
) -> bool:
    """Whether `daily_coverage` grades ``day`` complete for ``repo`` alone."""
    if not _covers(ranges, day):
        return False
    if metric in _RUN_METRICS and horizon is not None and day < horizon:
        return False
    return not (metric == WORKFLOWS and (repo, day) in missing_jobs)


@dataclass(frozen=True)
class RepositoryRow:
    """One repository's counts, and per metric the share of window days covered."""

    repo: str
    counts: Mapping[str, int]
    coverage: Mapping[str, float]

    def record(self) -> dict[str, object]:
        """The row as one record: ``repository``, each count and ``coverage``."""
        return {"repository": self.repo, **self.counts, "coverage": dict(self.coverage)}


def build_repositories(
    store: Store,
    config: Config,
    target: Target,
    *,
    until: date,
    identities: ResolvedIdentities | None = None,
) -> list[RepositoryRow]:
    """The rows of ``target``'s repository table, sorted by repository.

    A repository's coverage share for a metric is the number of window
    days graded ``complete`` for it alone, by the rules of
    `daily_coverage`, over the number of days in the window. The runs
    horizon and the days missing job counts are read once for all rows.
    """
    if identities is None:
        identities = resolve_identities(target, config)
    days = window(config.window.days, until)
    first, last = days[0], days[-1]
    members = public_members(store, identities, owner=target.owner)
    classes: dict[tuple[str | None, str | None], ActorClass | None] = {}

    def counted(login: str | None, account_type: str | None) -> bool:
        pair = (login, account_type)
        if pair not in classes:
            classes[pair] = classify(login, account_type, identities, members)
        return classes[pair] is not None

    scope = [set(store.in_scope(day)) for day in days]
    repos = sorted(set().union(*scope))
    counts: dict[str, dict[str, int]] = {
        repo: dict.fromkeys(COLUMNS, 0) for repo in repos
    }
    for column, (kind, field) in _ITEM_QUERIES.items():
        for repo, login, account_type, count in store.item_repository_counts(
            kind, field, first, last
        ):
            if repo in counts and counted(login, account_type):
                counts[repo][column] += count
    run_rows = store.run_repository_counts(first, last)
    for repo, login, account_type, automation, runs, jobs in run_rows:
        if repo in counts and (automation is not None or counted(login, account_type)):
            counts[repo]["runs"] += runs
            counts[repo]["jobs"] += jobs
    metrics = _enabled(config)
    horizon = runs_horizon(store, target.owner)
    missing_jobs = set(store.days_missing_jobs(first, last))
    rows = []
    for repo in repos:
        ranges = {
            store_metric: store.coverage(repo, store_metric)
            for store_metric in {COVERAGE_METRIC[metric] for metric in metrics}
        }
        in_scope = [
            day for day, day_scope in zip(days, scope, strict=True) if repo in day_scope
        ]
        shares = {
            metric: round(
                sum(
                    _complete(
                        metric,
                        repo,
                        day,
                        ranges[COVERAGE_METRIC[metric]],
                        horizon,
                        missing_jobs,
                    )
                    for day in in_scope
                )
                / len(days),
                _SHARE_DIGITS,
            )
            for metric in metrics
        }
        rows.append(
            RepositoryRow(
                repo, MappingProxyType(counts[repo]), MappingProxyType(shares)
            )
        )
    return rows
