# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""What every collector shares: repository keys, concurrency, status handling."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterable
from datetime import UTC, datetime
from typing import Any, TypeVar

import httpx

from github_statistics.client import AuthError
from github_statistics.client.policy import rate_limited
from github_statistics.scope import ResolvedScope
from github_statistics.store import Store

UNREADABLE_STATUSES = frozenset({403, 404, 410})
SERVER_ERROR = 500

T = TypeVar("T")


def scope_keys(store: Store, scope: ResolvedScope, now: datetime) -> list[str]:
    """The ``owner/name`` key of each repository in ``scope``.

    A repository keeps the spelling the store's scope holds for it, as
    names compare without case, so a rename in case alone keeps its
    sync state, records and coverage under one key.
    """
    spellings = {key.lower(): key for key in store.in_scope(now.astimezone(UTC).date())}
    return [
        spellings.get(key.lower(), key)
        for key in (
            f"{scope.owner}/{repository.name}" for repository in scope.repositories
        )
    ]


async def run_concurrently(syncs: Iterable[Coroutine[Any, Any, T]]) -> list[T]:
    """Await ``syncs`` concurrently; their results in order.

    An error that stops one, ``AuthError`` first, propagates as itself,
    not wrapped in an ``ExceptionGroup``, and cancels the others.
    """
    tasks = []
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(sync) for sync in syncs]
    except ExceptionGroup as failures:
        raise (failures.subgroup(AuthError) or failures).exceptions[0] from None
    return [task.result() for task in tasks]


def unreadable_status(exc: httpx.HTTPStatusError) -> int | None:
    """The status that makes a repository unreadable for this run, if any.

    ``None`` for a rate limit an unbudgeted transport returned after its
    retries, which stops the sync short like a spent budget. A ``403``
    that is not a rate limit, a ``404``, a ``410`` or a ``5xx`` is
    returned; any other status re-raises ``exc``.
    """
    status = exc.response.status_code
    if rate_limited(exc.response):
        return None
    if status in UNREADABLE_STATUSES or status >= SERVER_ERROR:
        return status
    raise exc
