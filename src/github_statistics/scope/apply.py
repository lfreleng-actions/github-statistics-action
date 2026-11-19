# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Record a resolved scope in the store (design §5, §6.1).

Store keys are ``owner/name``. A repository dropped as ``not public``
or ``not found`` is purged: a token that cannot see a private
repository gets a ``404`` for it, so ``not found`` cannot be told apart
from a repository that turned private.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from github_statistics.store import Store

from .resolver import NOT_FOUND, NOT_PUBLIC, ResolvedScope

PURGED_REASONS = frozenset({NOT_PUBLIC, NOT_FOUND})


@dataclass(frozen=True)
class ScopeChanges:
    """What `apply_scope` did, each as sorted ``owner/name`` keys.

    ``untouched`` holds repositories in scope before whose fate the
    resolver could not determine; they stay as they were.
    """

    entered: tuple[str, ...]
    left: tuple[str, ...]
    purged: tuple[str, ...]
    untouched: tuple[str, ...]


def apply_scope(
    store: Store, scope: ResolvedScope, *, today: date, window_start: date
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
    reported. Pass ``store.stored_repositories()`` to
    ``resolve_scope(known=...)`` so it accounts for each.
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
    return ScopeChanges(
        entered=tuple(sorted(entered)),
        left=tuple(sorted(left)),
        purged=tuple(sorted(purged)),
        untouched=tuple(sorted(untouched)),
    )
