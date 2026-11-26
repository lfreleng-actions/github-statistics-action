# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Record a resolved scope in the store (design §5, §6.1).

Store keys are ``owner/name``. A repository dropped as ``not public``
or ``not found`` is purged: a token that cannot see a private
repository gets a ``404`` for it, so ``not found`` cannot be told apart
from a repository that turned private. A repository out of scope whose
history is incomplete is purged too: no run reads it again, so every
day it was in scope would stay partial.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from github_statistics.store import Store

from .resolver import NOT_FOUND, NOT_PUBLIC, ResolvedScope

log = logging.getLogger(__name__)

PURGED_REASONS = frozenset({NOT_PUBLIC, NOT_FOUND})
LEFT_INCOMPLETE = "left incomplete"
DAY = timedelta(days=1)


@dataclass(frozen=True)
class ScopeChanges:
    """What `apply_scope` did, each as sorted ``owner/name`` keys.

    ``untouched`` holds repositories in scope before whose fate the
    resolver could not determine; they stay as they were. ``reasons``
    maps each of ``purged`` to why: its drop reason, or
    `LEFT_INCOMPLETE`.
    """

    entered: tuple[str, ...]
    left: tuple[str, ...]
    purged: tuple[str, ...]
    untouched: tuple[str, ...]
    reasons: Mapping[str, str] = field(default_factory=dict)


def _in_scope_days(store: Store, repo: str, first: date, last: date) -> list[date]:
    """The days from ``first`` to ``last`` on which ``repo`` was in scope."""
    days: set[date] = set()
    for from_day, to_day in store.scope_intervals(repo):
        day = max(from_day, first)
        end = last if to_day is None else min(last, to_day - DAY)
        while day <= end:
            days.add(day)
            day += DAY
    return sorted(days)


def _incomplete(
    store: Store, owner: str, metrics: Sequence[str], repos: Mapping[str, list[date]]
) -> set[str]:
    """Each of ``repos`` the coverage export grades short on a day (§7.3).

    Each of ``repos`` maps to the days it was in scope. Every one of
    ``metrics`` counts, graded by `daily_coverage` as if each repository
    were alone in scope; days before the owner's runs horizon are
    covered for no repository, so they count for no run metric. One
    `daily_coverage` call grades every repository, so the owner's run
    history and unread job counts are read once per pass.
    """
    # Imported here: the series package imports collect, which imports this one.
    from github_statistics.collect import runs_horizon
    from github_statistics.render.model import Coverage
    from github_statistics.series.coverage import (
        WORKFLOW_OUTCOMES,
        WORKFLOWS,
        daily_coverage,
    )

    entries = sorted((day, repo) for repo, days in repos.items() for day in days)
    if not entries or not metrics:
        return set()
    run_metrics = {WORKFLOWS, WORKFLOW_OUTCOMES}
    horizon = runs_horizon(store, owner) if run_metrics & set(metrics) else None
    grades = daily_coverage(
        store, owner, metrics, [day for day, _ in entries], [[r] for _, r in entries]
    )
    short: set[str] = set()
    for metric, values in grades.items():
        excused = horizon if metric in run_metrics else None
        for (day, repo), grade in zip(entries, values, strict=True):
            if excused is not None and day < excused:
                continue
            if grade is not Coverage.COMPLETE:
                short.add(repo)
    return short


def _purge_incomplete(
    store: Store,
    scope: ResolvedScope,
    metrics: Sequence[str],
    *,
    today: date,
    first: date,
    last: date,
) -> list[str]:
    """Purge each departed repository of ``scope.owner`` left incomplete.

    Departed means stored, out of scope ``today`` and not among
    ``scope.unresolved``; incomplete means `_incomplete` grades some
    of ``metrics`` short on a day from ``first`` to ``last`` it was in
    scope. Every departed repository is graded before the first purge,
    so the outcome does not depend on order. Every spelling of its name
    goes; returns the purged keys.
    """
    owner = scope.owner
    names = {key.lower(): key for key in (f"{owner}/{d.name}" for d in scope.dropped)}
    skipped = {f"{owner}/{name}".lower() for name in scope.unresolved}
    skipped.update(repo.lower() for repo in store.in_scope(today))
    departed: dict[str, set[str]] = {}
    for repo in store.stored_repositories():
        lowered = repo.lower()
        if lowered.startswith(f"{owner.lower()}/") and lowered not in skipped:
            departed.setdefault(lowered, set()).add(repo)
    short = _incomplete(
        store,
        owner,
        metrics,
        {
            repo: _in_scope_days(store, repo, first, last)
            for stored in departed.values()
            for repo in stored
        },
    )
    incomplete = [
        (lowered, stored)
        for lowered, stored in sorted(departed.items())
        if short & stored
    ]
    purged: list[str] = []
    for lowered, stored in incomplete:
        for spelling in sorted(stored):
            store.purge_repository(spelling)
        key = names.get(lowered, min(stored))
        log.info("purged %s: %s", key, LEFT_INCOMPLETE)
        purged.append(key)
    return purged


def apply_scope(
    store: Store,
    scope: ResolvedScope,
    *,
    today: date,
    window_start: date,
    window_end: date | None = None,
    metrics: Sequence[str] = (),
) -> ScopeChanges:
    """Bring the store's scope table in line with ``scope``.

    A repository never seen before enters from ``window_start`` or its
    creation day, whichever is later, so its backfilled history counts;
    one that returns enters from ``today``. One in scope before that
    ``scope`` drops leaves from ``today`` and keeps its history, unless
    it is dropped as ``not public`` or ``not found``: then every row it
    has is purged, under every spelling of its name the store holds,
    since names compare without case. One in scope before
    that ``scope`` neither keeps nor drops is left untouched and
    reported. Then every stored repository of the owner out of scope
    ``today``, whether it left now or on an earlier run, and not left
    unresolved, is purged under every spelling when some day it was in
    scope, from ``window_start`` to ``window_end`` (by default the day
    before ``today``), is not complete for one of the charted
    ``metrics`` by the rule the coverage export grades by; with no
    ``metrics`` none is. Pass
    ``store.stored_repositories()`` to ``resolve_scope(known=...)`` so
    it accounts for each.
    """
    owner = scope.owner
    current = {repo.lower(): repo for repo in store.in_scope(today)}
    spellings: dict[str, set[str]] = {}
    for repo in store.stored_repositories():
        spellings.setdefault(repo.lower(), set()).add(repo)
    entered: list[str] = []
    kept: set[str] = set()
    for repository in scope.repositories:
        key = f"{owner}/{repository.name}"
        kept.add(key.lower())
        if key.lower() in current:
            continue
        created = date.fromisoformat(repository.created_at[:10])
        start = today if key.lower() in spellings else max(window_start, created)
        store.enter_scope(key, start)
        entered.append(key)

    reasons = {f"{owner}/{drop.name}".lower(): drop.reason for drop in scope.dropped}
    purged: list[str] = []
    for drop in scope.dropped:
        if drop.reason in PURGED_REASONS:
            key = f"{owner}/{drop.name}"
            for spelling in sorted(spellings.get(key.lower(), set()) | {key}):
                store.purge_repository(spelling)
            purged.append(key)

    left: list[str] = []
    untouched: list[str] = []
    for lowered, key in current.items():
        if lowered in kept:
            continue
        reason = reasons.get(lowered)
        if reason is None:
            untouched.append(key)
        elif reason not in PURGED_REASONS:
            store.leave_scope(key, today)
            left.append(key)
    why = {key: reasons[key.lower()] for key in purged}
    last = today - DAY if window_end is None else window_end
    for key in _purge_incomplete(
        store, scope, metrics, today=today, first=window_start, last=last
    ):
        purged.append(key)
        why[key] = LEFT_INCOMPLETE
        left = [repo for repo in left if repo.lower() != key.lower()]
    return ScopeChanges(
        entered=tuple(sorted(entered)),
        left=tuple(sorted(left)),
        purged=tuple(sorted(purged)),
        untouched=tuple(sorted(untouched)),
        reasons=dict(sorted(why.items())),
    )
