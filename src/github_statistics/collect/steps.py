# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The step bookkeeping of one `collect` call (design §6.5).

`Steps` runs each step's work, ends a step cleanly on a spent budget
or an outage, and records the first step the budget ran out in, the
unreadable repositories and the stalled ones. `each` runs one step's
work for many repositories at once and keeps what finished.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Coroutine, Iterable, Mapping
from typing import Any, TypeVar

import httpx

from github_statistics.client import (
    BudgetExhaustedError,
    GraphQLError,
    Transport,
    TransportError,
)

from .common import SERVER_ERROR, run_concurrently
from .items import ItemSyncResult
from .runs import _RunSync

log = logging.getLogger(__name__)

T = TypeVar("T")


class Steps:
    """The step bookkeeping of one `collect` call."""

    def __init__(self, transport: Transport) -> None:
        """Track ``transport``'s budget across the steps."""
        self.transport = transport
        self.exhausted: str | None = None
        self.unreadable: dict[str, str] = {}
        self.stalled: set[str] = set()

    async def run(self, step: str, work: Awaitable[T]) -> T | None:
        """Await ``work`` as part of ``step``; ``None`` if it ended early.

        A spent budget or an outage, a request that outlasted its retries
        or a REST or GraphQL ``5xx``, ends that work, never the run;
        ``AuthError``, any other HTTP status error and any other
        ``GraphQLError``, such as a refused query, propagate.
        """
        try:
            value: T | None = await work
        except BudgetExhaustedError as exc:
            log.info("%s step stopped: %s", step, exc)
            self.stop(step)
            value = None
        except TransportError as exc:
            log.warning("%s step ended: %s", step, exc)
            value = None
        except GraphQLError as exc:
            if exc.status is None or exc.status < SERVER_ERROR:
                raise
            log.warning("%s step ended: %s", step, exc)
            value = None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < SERVER_ERROR:
                raise
            log.warning("%s step ended: %s", step, exc)
            value = None
        budget = self.transport.budget
        if budget is not None and (budget.rest_exhausted or budget.graphql_exhausted):
            self.stop(step)
        return value

    async def reading(self, key: str, what: str, work: Coroutine[Any, Any, T]) -> T:
        """Await one repository's ``work``; on an outage, record ``key`` first.

        A ``TransportError`` marks ``key`` unreadable for ``what`` and
        propagates, so it still ends the step.
        """
        try:
            return await work
        except TransportError as exc:
            self.unreadable.setdefault(key, f"{what}: {exc}")
            raise

    def stop(self, step: str) -> None:
        """Record ``step`` as the one the budget ran out in, unless one was."""
        if self.exhausted is None:
            log.info("budget spent in the %s step; the next run resumes", step)
            self.exhausted = step

    def items(self, step: str, results: Mapping[str, ItemSyncResult] | None) -> None:
        """Record an item sync's unreadable repositories and budget stops.

        A listing neither drained nor unreadable was stopped by a spent
        budget or a rate limit, which the budget counters may not show.
        """
        for key, result in (results or {}).items():
            if result.unreadable is not None:
                self.unreadable[key] = f"items: HTTP {result.unreadable}"
            elif not result.drained:
                self.stop(step)

    def phases(
        self, step: str, syncs: Iterable[_RunSync], finished: Iterable[bool | None]
    ) -> None:
        """Record a budget stop of run phases that ended early and readable.

        A phase cancelled when its step ended, ``None``, records nothing.
        """
        for sync, done in zip(syncs, finished, strict=False):
            if done is False and sync.result().unreadable is None:
                self.stop(step)

    def failed(self, what: str, keys: Iterable[str]) -> None:
        """Record each repository in ``keys`` as unreadable for ``what``.

        ``keys`` holds a repository once per record of it that failed.
        """
        for key, count in Counter(keys).items():
            self.unreadable.setdefault(key, f"{what}: {count} failed")

    def runs(self, key: str, unreadable: int | None, stalled: bool) -> None:
        """Record a run read's unreadable status and stall."""
        if unreadable is not None:
            self.unreadable.setdefault(key, f"runs: HTTP {unreadable}")
        if stalled:
            self.stalled.add(key)


async def each(
    steps: Steps, step: str, works: Iterable[Coroutine[Any, Any, T]]
) -> list[T | None]:
    """Await ``works`` concurrently as ``step``; each result, ``None`` if cut short.

    A failure that ends the step cancels the works still running, but
    those that finished keep their results, so the later steps still
    run for their repositories and report what they found.
    """
    pending = list(works)
    results: list[T | None] = [None] * len(pending)

    async def keep(index: int, work: Coroutine[Any, Any, T]) -> None:
        """Await ``work`` and keep its result at ``index``."""
        results[index] = await work

    await steps.run(
        step, run_concurrently(keep(i, work) for i, work in enumerate(pending))
    )
    return results
