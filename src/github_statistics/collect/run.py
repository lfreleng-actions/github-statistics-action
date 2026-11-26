# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""One target's budgeted, self-healing sync (design §6.5).

`collect` runs the collectors in a fixed order, sharing the
transport's one budget: scope, public members, the incremental reads,
enrichment, backfill, enrichment of what the backfill stored, then the
re-run sweep. The incremental reads and the first enrichment always
come before backfill, so yesterday is complete while history still
fills in. Each step's listing sync ends before the next step starts,
so enrichment never runs alongside a listing (design §6.3).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from github_statistics.client import Transport
from github_statistics.config import Config, Identities, Target, resolve_identities
from github_statistics.scope import ResolvedScope, apply_scope, resolve_scope
from github_statistics.store import Store

from .common import scope_keys
from .enrich_items import PER_REPOSITORY, enrich_items
from .items import ITEMS_METRIC, sync_all_items
from .jobs import ALL_RUNS, enrich_jobs
from .members import sync_members
from .runs import _RunSync
from .steps import Steps, each
from .sweep import sweep_runs

DAY = timedelta(days=1)
SCOPE = "scope"
MEMBERS = "members"
INCREMENTAL = "incremental"
ENRICHMENT = "enrichment"
BACKFILL = "backfill"
SWEEP = "sweep"
# The values of the series coverage grades (`render.model.Coverage`).
COMPLETE, PARTIAL, NONE = "complete", "partial", "none"


@dataclass(frozen=True)
class MetricCoverage:
    """How many days of the window one charted metric has, by grade (§7.3)."""

    complete: int
    partial: int
    missing: int


@dataclass(frozen=True)
class CollectResult:
    """What one target's sync did and how much of the window it holds.

    ``unreadable`` and ``dropped`` map ``owner/name`` keys to why.
    ``exhausted`` names the first step that ended with the REST or the
    GraphQL budget spent, or in which a collector reported a stop for a
    spent budget or a rate limit, else ``None``. ``stalled`` names repositories
    whose run read a spent budget stopped inside one second.
    ``gap_days`` counts the window's days on which any enabled metric is
    not complete.
    """

    owner: str
    requests: int
    graphql_points: int
    not_modified: int
    coverage: Mapping[str, MetricCoverage] = field(default_factory=dict)
    gap_days: int = 0
    unreadable: Mapping[str, str] = field(default_factory=dict)
    dropped: Mapping[str, str] = field(default_factory=dict)
    exhausted: str | None = None
    stalled: tuple[str, ...] = ()

    @property
    def coverage_complete(self) -> bool:
        """Whether every day of every enabled metric is complete."""
        return self.gap_days == 0


def _only(scope: ResolvedScope, keys: Iterable[str]) -> ResolvedScope:
    """``scope`` with only the repositories whose key is among ``keys``."""
    wanted = {key.lower() for key in keys}
    repositories = tuple(
        repository
        for repository in scope.repositories
        if f"{scope.owner}/{repository.name}".lower() in wanted
    )
    return dataclasses.replace(scope, repositories=repositories)


def _refresh(store: Store, keys: Iterable[str]) -> None:
    """Clear the sync state and coverage of ``keys``, keeping their rows."""
    connection = store.connection
    connection.execute("BEGIN")
    try:
        for key in keys:
            for table in ("sync_state", "coverage"):
                connection.execute(f"DELETE FROM {table} WHERE repo = ?", (key,))
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def window_coverage(
    store: Store, owner: str, metrics: list[str], start: date, end: date
) -> tuple[dict[str, MetricCoverage], int]:
    """Each metric's days from ``start`` to ``end`` by grade; the gap days.

    A gap day is one on which any of ``metrics`` is not complete.
    """
    # Imported here: the series package imports this one, and loads render.
    from github_statistics.series.coverage import daily_coverage

    days = [start + DAY * offset for offset in range((end - start).days + 1)]
    scope = [store.in_scope(day) for day in days]
    grades = daily_coverage(store, owner, metrics, days, scope)
    coverage = {
        metric: MetricCoverage(
            complete=values.count(COMPLETE),
            partial=values.count(PARTIAL),
            missing=values.count(NONE),
        )
        for metric, values in grades.items()
    }
    gaps = sum(
        any(grades[metric][index] != COMPLETE for metric in metrics)
        for index in range(len(days))
    )
    return coverage, gaps


@dataclass(frozen=True)
class _Plan:
    """What one `collect` call reads: the scope, its keys and the window."""

    scope: ResolvedScope
    keys: list[str]
    window_start: date
    now: datetime
    items: bool
    runs: bool
    jobs: bool


async def _sync(steps: Steps, transport: Transport, store: Store, plan: _Plan) -> None:
    """Steps 3 to 7 of `collect` over ``plan``'s repositories."""
    window, now = {"window_start": plan.window_start}, plan.now

    def fresh(key: str) -> bool:
        """Whether ``key`` has no item watermark yet, so needs a first sync."""
        state = store.sync_state(key, ITEMS_METRIC)
        return state is None or state.watermark is None

    first = [key for key in plan.keys if fresh(key)]
    later = [key for key in plan.keys if not fresh(key)]
    syncs = [_RunSync(transport, store, key, **window, now=now) for key in plan.keys]
    current: list[_RunSync] = []

    async def items(step: str, keys: list[str]) -> None:
        """Sync the items of ``keys``, each under `Steps.reading`."""
        listed = await each(
            steps,
            step,
            (
                steps.reading(
                    key,
                    "items",
                    sync_all_items(
                        transport, store, _only(plan.scope, [key]), **window, now=now
                    ),
                )
                for key in keys
            ),
        )
        steps.items(step, {k: v for part in listed if part for k, v in part.items()})

    def guarded(
        sync: _RunSync, phase: Callable[[], Awaitable[None]]
    ) -> Coroutine[Any, Any, bool]:
        """``sync``'s ``phase``, guarded, under `Steps.reading`."""
        return steps.reading(sync.key, "runs", sync.guarded(phase))

    if plan.items and later:
        await items(INCREMENTAL, later)
    if plan.runs:
        finished = await each(
            steps, INCREMENTAL, (guarded(sync, sync.recent) for sync in syncs)
        )
        steps.phases(INCREMENTAL, syncs, finished)
        current = [s for s, done in zip(syncs, finished, strict=True) if done]

    await _enrich(
        steps,
        transport,
        store,
        plan.keys if plan.items else [],
        plan.keys if plan.jobs else [],
    )

    if plan.items and first:
        await items(BACKFILL, first)
    stored: list[str] = []
    if current:
        before = {sync.key: sync.result().runs for sync in current}
        backfilled = await each(
            steps, BACKFILL, (guarded(sync, sync.backfill) for sync in current)
        )
        steps.phases(BACKFILL, current, backfilled)
        stored = [s.key for s in current if s.result().runs > before[s.key]]
    for sync in syncs:
        result = sync.result()
        steps.runs(sync.key, result.unreadable, result.stalled)

    await _enrich(
        steps,
        transport,
        store,
        first if plan.items else [],
        stored if plan.jobs else [],
    )

    if current:
        await _sweep(steps, transport, store, [sync.key for sync in current], now)


async def _enrich(
    steps: Steps,
    transport: Transport,
    store: Store,
    items: list[str],
    runs: list[str],
) -> None:
    """Steps 4 and 6: enrich the items of ``items``, then count the jobs of ``runs``.

    Either list may be empty, and skips its half. Reports the
    repositories they failed: an id that failed alone, unlike one the
    budget left, makes the repository holding it unreadable.
    """
    if items:
        enriched = await steps.run(ENRICHMENT, enrich_items(transport, store, items))
        if enriched is not None and enriched.failed:
            ids = set(enriched.failed)
            steps.failed(
                "item enrichment",
                (
                    item.repo
                    for key in items
                    for item in store.items_needing_enrichment(key, PER_REPOSITORY)
                    if item.node_id in ids
                ),
            )
    if runs:
        counted = await steps.run(ENRICHMENT, enrich_jobs(transport, store, runs))
        if counted is not None and counted.failed:
            ids = set(counted.failed)
            needing = store.runs_needing_jobs(ALL_RUNS)
            steps.failed("job counts", (r.repo for r in needing if r.node_id in ids))


async def _sweep(
    steps: Steps, transport: Transport, store: Store, keys: list[str], now: datetime
) -> None:
    """Step 7: the re-run sweep of ``keys``, where due."""
    sweeps = await each(
        steps,
        SWEEP,
        (
            steps.reading(
                key, "runs", sweep_runs(transport, store, *key.split("/", 1), now=now)
            )
            for key in keys
        ),
    )
    for swept in sweeps:
        if swept is None:
            continue
        steps.runs(swept.repo, swept.unreadable, swept.stalled)
        if swept.due and not swept.swept and swept.unreadable is None:
            steps.stop(SWEEP)


async def collect(
    config: Config,
    target: Target,
    *,
    now: datetime,
    transport: Transport,
    store: Store,
    identities: Identities | None = None,
    refresh: bool = False,
    until: date | None = None,
) -> CollectResult:
    """Sync ``target`` into ``store`` in the order design §6.5 gives.

    The window is ``config.window.days`` days ending on ``until``,
    by default the day before ``now``, in UTC; ``now`` stays the time
    of the reads. Steps: resolve the scope and apply it to the store;
    read public members; the incremental reads of items, from
    repositories with a watermark, then of runs; enrich items, then
    count jobs; backfill, first item syncs then runs; enrich again the
    items of those first syncs and count the jobs of the runs the
    backfill stored, whatever REST budget it left; the re-run sweep,
    where due. A disabled metric's collectors do not run. An empty scope
    makes no call after its resolution, and a scope the budget stops
    applies nothing. A repository whose read outlasts its retries is
    unreadable, and the outage ends its step; the repositories that
    finished that step go on to the later ones. A repository with an
    item or a run whose enrichment read fails alone is unreadable too.
    ``refresh`` clears the
    scope's sync state and coverage first, so the window is read again.
    One repository failing never fails the run, and a spent budget ends
    the steps cleanly; ``AuthError`` propagates.
    """
    today = now.astimezone(UTC).date()
    end = until or today - DAY
    start = end - DAY * (config.window.days - 1)
    metrics = config.metrics
    enabled = [f.name for f in dataclasses.fields(metrics) if getattr(metrics, f.name)]
    steps = Steps(transport)
    dropped: dict[str, str] = {}

    known = store.stored_repositories()
    scope = await steps.run(SCOPE, resolve_scope(transport, target, known=known))
    owner = target.owner if scope is None else scope.owner
    if scope is not None:
        apply_scope(store, scope, today=today, window_start=start)
        dropped = {f"{owner}/{drop.name}": drop.reason for drop in scope.dropped}
        for name in scope.unresolved:
            steps.unreadable[f"{owner}/{name}"] = "scope: read failed"
        keys = scope_keys(store, scope, now)
        if refresh:
            _refresh(store, keys)
        if keys:
            orgs = resolve_identities(target, config, identities).member_orgs.values
            await steps.run(
                MEMBERS,
                sync_members(
                    transport,
                    store,
                    orgs,
                    owner=owner,
                    owner_type=scope.owner_type,
                    today=today,
                ),
            )
            plan = _Plan(
                scope,
                keys,
                start,
                now,
                items=metrics.pull_requests or metrics.issues,
                runs=metrics.workflows or metrics.workflow_outcomes,
                jobs=metrics.workflows,
            )
            await _sync(steps, transport, store, plan)

    coverage, gaps = window_coverage(store, owner, enabled, start, end)
    stats = transport.stats
    return CollectResult(
        owner=owner,
        requests=stats.requests,
        graphql_points=stats.graphql_cost,
        not_modified=stats.not_modified,
        coverage=coverage,
        gap_days=gaps,
        unreadable=dict(sorted(steps.unreadable.items())),
        dropped=dropped,
        exhausted=steps.exhausted,
        stalled=tuple(sorted(steps.stalled)),
    )
