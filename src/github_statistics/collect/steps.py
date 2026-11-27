# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The step bookkeeping of one `collect` call (design §6.5).

`Steps` runs each step's work, ends a step cleanly on a spent budget
or an outage, and records the first step the budget ran out in, the
unreadable repositories and the stalled ones. `each` runs one step's
work for many repositories at once and keeps what finished.

Progress goes to the log at INFO: a line when each step starts and
ends, with the budget spent and left, and a line per repository per
step. Lines name a repository and counts, never a URL. A step may list
a repository's items and, after other steps, read its runs, so an item
line waits to join that step's run counts for the repository. It comes
alone once the step's run phase ends without it, once the repository's
earlier run phase stops short, or once a step reading no repository
ends before any run phase, when the call reads no runs.
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

from .common import SERVER_ERROR, run_concurrently, stop_cause
from .items import ItemSyncResult
from .runs import _RunSync
from .sweep import SweepResult

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
        self._step = ""
        self._read = False
        self._phased = False
        self._reported: set[tuple[str, str]] = set()
        self._held: dict[tuple[str, str], str] = {}
        self._runs: dict[str, tuple[int, int]] = {}

    def _spent(self) -> str:
        """What the transport has spent so far, and what its budget leaves."""
        stats, budget = self.transport.stats, self.transport.budget
        if budget is None:
            return (
                f"{stats.requests} HTTP requests and {stats.graphql_cost} "
                "GraphQL points spent; no budget set"
            )
        return (
            f"{budget.rest_spent} REST requests and {budget.graphql_spent} "
            f"GraphQL points spent; {budget.rest_left} requests and "
            f"{budget.graphql_left} points left"
        )

    def _report(
        self,
        step: str,
        key: str,
        what: str,
        counts: tuple[int, int],
        hold: bool = False,
    ) -> None:
        """Log what ``key`` read in ``step``, at most once per step.

        ``counts`` holds the pages read and the records stored. With
        ``hold``, the line waits to join a later report of ``key`` in
        ``step``, or for `_release`.
        """
        if (step, key) in self._reported:
            return
        part = f"{what} read over {counts[0]} pages, {counts[1]} stored"
        if hold:
            self._held.setdefault((step, key), part)
            return
        held = self._held.pop((step, key), None)
        self._reported.add((step, key))
        log.info("%s step: %s: %s", step, key, "; ".join(filter(None, (held, part))))

    def _release(self, step: str | None = None, key: str | None = None) -> None:
        """Log the held lines of ``step`` and of ``key``; with neither, all."""
        due = [h for h in self._held if step in (None, h[0]) and key in (None, h[1])]
        for held in due:
            part = self._held.pop(held)
            self._reported.add(held)
            log.info("%s step: %s: %s", *held, part)

    async def run(self, step: str, work: Awaitable[T]) -> T | None:
        """Await ``work`` as part of ``step``; ``None`` if it ended early.

        A spent budget or an outage, a request that outlasted its retries
        or a REST or GraphQL ``5xx``, ends that work, never the run;
        ``AuthError``, any other HTTP status error and any other
        ``GraphQLError``, such as a refused query, propagate.
        """
        self._step, self._read = step, False
        log.info("starting the %s step; %s", step, self._spent())
        try:
            value: T | None = await work
        except BudgetExhaustedError as exc:
            log.info("%s step stopped: %s", step, stop_cause(self.transport, exc))
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
        log.info("finished the %s step; %s", step, self._spent())
        if not self._read and not self._phased:
            self._release()
        return value

    async def reading(self, key: str, what: str, work: Coroutine[Any, Any, T]) -> T:
        """Await one repository's ``work``; on an outage, record ``key`` first.

        A ``TransportError`` marks ``key`` unreadable for ``what`` and
        propagates, so it still ends the step. A finished sweep logs its
        progress line here, as soon as it finishes; a finished item sync
        holds its line for the step's run counts. A run phase returns
        only whether it finished, so `phases` logs it.
        """
        self._read = True
        try:
            value = await work
        except TransportError as exc:
            self.unreadable.setdefault(key, f"{what}: {exc}")
            raise
        if isinstance(value, SweepResult):
            self._report(self._step, key, what, (value.pages, value.runs))
        elif isinstance(value, Mapping):
            for found in value.values():
                if isinstance(found, ItemSyncResult):
                    counts = (found.pages, found.items)
                    self._report(self._step, found.repo, what, counts, hold=True)
        return value

    def stop(self, step: str) -> None:
        """Record ``step`` as the one the budget ran out in, unless one was.

        The line names the time limit when the deadline, not a count,
        stopped the run first.
        """
        if self.exhausted is None:
            budget = self.transport.budget
            cause = "budget spent"
            if budget is not None and budget.stop_reason() == "time":
                cause = "time limit reached"
            log.info("%s in the %s step; the next run resumes", cause, step)
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
        Each other phase logs the runs and pages it added, joined to
        the items its repository listed in ``step``. A repository whose
        phase did not finish reads no further runs, so its other held
        item lines follow, and then the rest of ``step``'s.
        """
        self._phased = True
        for sync, done in zip(syncs, finished, strict=False):
            if done is not None:
                result = sync.result()
                runs, pages = self._runs.get(sync.key, (0, 0))
                self._runs[sync.key] = (result.runs, result.pages)
                counts = (result.pages - pages, result.runs - runs)
                self._report(step, sync.key, "runs", counts)
            if done is not True:
                self._release(key=sync.key)
            if done is False and sync.result().unreadable is None:
                self.stop(step)
        self._release(step=step)

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
