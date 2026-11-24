# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Daily series per metric, series and actor class (design §7.3).

Every count goes to the author of the item, or to the triggering actor
of a workflow run; a scheduled run goes to ``org_bot`` and to no login.
Pure and deterministic: the window end is passed in, and the store is
the only input besides the configuration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from types import MappingProxyType

from ..config import Config, ResolvedIdentities, Target, resolve_identities
from ..render.model import Coverage
from ..store import Store
from .actors import (
    AGGREGATES,
    CLASSES,
    ActorClass,
    classify,
    public_members,
    unclassified_bots,
)
from .coverage import (
    ISSUES,
    PULL_REQUESTS,
    WORKFLOW_OUTCOMES,
    WORKFLOWS,
    daily_coverage,
)

OPENED = "opened"
CLOSED = "closed"
RUNS = "runs"
JOBS = "jobs"
SUCCEEDED = "succeeded"
FAILED = "failed"

SERIES: dict[str, tuple[str, str]] = {
    PULL_REQUESTS: (OPENED, CLOSED),
    ISSUES: (OPENED, CLOSED),
    WORKFLOWS: (RUNS, JOBS),
    WORKFLOW_OUTCOMES: (SUCCEEDED, FAILED),
}
SUCCESS_CONCLUSIONS = frozenset({"success"})
FAILURE_CONCLUSIONS = frozenset({"failure", "startup_failure", "timed_out"})
_ITEM_KINDS = {PULL_REQUESTS: "pr", ISSUES: "issue"}
_ITEM_COLUMNS = {OPENED: "created_at", CLOSED: "closed_at"}

SeriesKey = tuple[str, str, str]


@dataclass(frozen=True)
class TargetSeries:
    """Everything the charts of one target draw, one value per day of ``days``.

    ``counts`` maps ``(metric, series, class)`` to a vector, for each of
    the four classes and the ``humans``, ``bots`` and ``all`` aggregates;
    ``coverage`` maps each metric to its per-day coverage;
    ``repositories`` counts the repositories in scope each day.
    """

    owner: str
    days: tuple[date, ...]
    counts: Mapping[SeriesKey, tuple[int, ...]]
    coverage: Mapping[str, tuple[Coverage, ...]]
    repositories: tuple[int, ...]
    unclassified_bots: tuple[str, ...]

    def values(self, metric: str, series: str, actor_class: str) -> tuple[int, ...]:
        """The vector of one metric, series and class or aggregate."""
        return self.counts[metric, series, str(actor_class)]


def window(days: int, until: date) -> tuple[date, ...]:
    """The ``days`` UTC days ending on ``until``, oldest first."""
    return tuple(until - timedelta(days=offset) for offset in range(days - 1, -1, -1))


class _Counter:
    """Per-class vectors over a window, filled one classified count at a time."""

    def __init__(
        self,
        days: tuple[date, ...],
        identities: ResolvedIdentities,
        members: frozenset[str],
    ) -> None:
        """Start every vector at zero; classes resolve once per ``(login, type)``."""
        self._index = {day: position for position, day in enumerate(days)}
        self._width = len(days)
        self._identities = identities
        self._members = members
        self._classes: dict[tuple[str | None, str | None], ActorClass | None] = {}
        self.vectors: dict[SeriesKey, list[int]] = {}

    def start(self, metric: str, series: str) -> None:
        """Give ``metric`` and ``series`` a zero vector for every class."""
        for actor_class in CLASSES:
            self.vectors[metric, series, actor_class.value] = [0] * self._width

    def classify(
        self, login: str | None, account_type: str | None
    ) -> ActorClass | None:
        """The class of one actor, computed once per distinct pair."""
        pair = (login, account_type)
        if pair not in self._classes:
            self._classes[pair] = classify(
                login, account_type, self._identities, self._members
            )
        return self._classes[pair]

    def add(
        self, metric: str, series: str, actor_class: ActorClass, day: date, count: int
    ) -> None:
        """Add ``count`` on ``day`` to one class's vector."""
        self.vectors[metric, series, actor_class.value][self._index[day]] += count

    def result(self) -> dict[SeriesKey, tuple[int, ...]]:
        """Every class vector plus the aggregates, as tuples."""
        counts: dict[SeriesKey, tuple[int, ...]] = {
            key: tuple(vector) for key, vector in self.vectors.items()
        }
        pairs = {(metric, series) for metric, series, _ in self.vectors}
        for metric, series in pairs:
            for name, members in AGGREGATES.items():
                columns = [self.vectors[metric, series, c.value] for c in members]
                counts[metric, series, name] = tuple(
                    sum(day) for day in zip(*columns, strict=True)
                )
        return counts


def _enabled(config: Config) -> list[str]:
    """The metrics the configuration charts, in a fixed order."""
    flags = config.metrics
    enabled = {
        PULL_REQUESTS: flags.pull_requests,
        ISSUES: flags.issues,
        WORKFLOWS: flags.workflows,
        WORKFLOW_OUTCOMES: flags.workflow_outcomes,
    }
    return [metric for metric, on in enabled.items() if on]


def _count_items(
    store: Store, counter: _Counter, metric: str, days: tuple[date, ...]
) -> None:
    """Fill the opened and closed vectors of one item metric."""
    for series in SERIES[metric]:
        counter.start(metric, series)
        rows = store.item_day_counts(
            _ITEM_KINDS[metric], _ITEM_COLUMNS[series], days[0], days[-1]
        )
        for row in rows:
            actor_class = counter.classify(row.login, row.account_type)
            if actor_class is not None:
                counter.add(metric, series, actor_class, row.day, row.count)


def _count_runs(
    store: Store, counter: _Counter, metrics: list[str], days: tuple[date, ...]
) -> None:
    """Fill the workflow and workflow outcome vectors the configuration charts."""
    wanted = [metric for metric in (WORKFLOWS, WORKFLOW_OUTCOMES) if metric in metrics]
    if not wanted:
        return
    for metric in wanted:
        for series in SERIES[metric]:
            counter.start(metric, series)
    for row in store.run_day_counts(days[0], days[-1]):
        if row.scheduled:
            actor_class: ActorClass | None = ActorClass.ORG_BOT
        else:
            actor_class = counter.classify(row.login, row.account_type)
        if actor_class is None:
            continue
        if WORKFLOWS in wanted:
            counter.add(WORKFLOWS, RUNS, actor_class, row.day, row.runs)
            counter.add(WORKFLOWS, JOBS, actor_class, row.day, row.jobs)
        if WORKFLOW_OUTCOMES in wanted:
            if row.conclusion in SUCCESS_CONCLUSIONS:
                counter.add(
                    WORKFLOW_OUTCOMES, SUCCEEDED, actor_class, row.day, row.runs
                )
            elif row.conclusion in FAILURE_CONCLUSIONS:
                counter.add(WORKFLOW_OUTCOMES, FAILED, actor_class, row.day, row.runs)


def build_series(
    store: Store,
    config: Config,
    target: Target,
    *,
    until: date,
    identities: ResolvedIdentities | None = None,
) -> TargetSeries:
    """The daily series, coverage and repository counts of ``target``.

    The window is ``config.window.days`` days ending on ``until``.
    ``identities`` defaults to the lists `resolve_identities` gives for
    ``target``; pass them to apply command-line overrides.
    """
    if identities is None:
        identities = resolve_identities(target, config)
    days = window(config.window.days, until)
    metrics = _enabled(config)
    members = public_members(store, identities, owner=target.owner)
    counter = _Counter(days, identities, members)
    for metric in (PULL_REQUESTS, ISSUES):
        if metric in metrics:
            _count_items(store, counter, metric, days)
    _count_runs(store, counter, metrics, days)
    scope = [store.in_scope(day) for day in days]
    return TargetSeries(
        owner=target.owner,
        days=days,
        counts=MappingProxyType(counter.result()),
        coverage=MappingProxyType(
            daily_coverage(store, target.owner, metrics, days, scope)
        ),
        repositories=tuple(len(repos) for repos in scope),
        unclassified_bots=tuple(unclassified_bots(store, identities)),
    )
