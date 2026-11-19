# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""A rate-limit pause that every request for the same resource waits out."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import httpx

# The key that pauses or stops every resource at once: a secondary rate
# limit spans REST and GraphQL alike.
ALL = "*"


class Cooldown:
    """Which rate-limit resources a transport is backing off from.

    While a rate limit's backoff (its ``Retry-After``, or the request's
    own backoff) runs, the resource it drew on, ``core``, ``search`` or
    ``graphql``, or ``ALL`` for a secondary limit, is paused: every
    attempt for it, queued or a retry, waits until the backoff ends
    rather than sending into the limit. The pause belongs to the limit,
    not to the request that met it, so it runs to its end even when
    that request is cancelled. That wait is not counted against the
    waiting request's own wait budget; the pause it waits on is bounded
    by the backoff of the request that met the limit.

    A rate limit that outlasts a request's retries stops its resource,
    or ``ALL``, for the rest of the run: ``stopped`` stays true, so no
    later attempt sends into a limit already known to hold.
    """

    def __init__(self, base_path: str) -> None:
        """Track pauses for an API whose paths start with ``base_path``."""
        self._base = base_path.rstrip("/")
        self._holders: dict[str, int] = {}
        self._open: dict[str, asyncio.Event] = {}
        self._stopped: set[str] = set()
        self._pauses: set[asyncio.Future[None]] = set()

    def resource(self, target: httpx.URL, graphql: bool) -> str:
        """The rate-limit resource a request to ``target`` draws on."""
        if graphql:
            return "graphql"
        path = target.path
        if path.startswith(self._base + "/"):
            path = path[len(self._base) :]
        return "search" if path.startswith("/search/") else "core"

    def _event(self, key: str) -> asyncio.Event:
        """The event that is set while ``key`` is not paused."""
        event = self._open.get(key)
        if event is None:
            event = self._open[key] = asyncio.Event()
            event.set()
        return event

    def pause(self, key: str, backoff: Awaitable[None]) -> asyncio.Future[None]:
        """Pause ``key`` from now until ``backoff`` finishes, and return it.

        The pause starts before this returns, so no queued attempt slips
        past, and ``backoff`` runs as a task of its own: a caller that
        awaits it through ``asyncio.shield`` can be cancelled without
        ending the pause early.
        """
        self._holders[key] = self._holders.get(key, 0) + 1
        self._event(key).clear()
        task = asyncio.ensure_future(backoff)
        self._pauses.add(task)

        def release(done: asyncio.Future[None]) -> None:
            self._pauses.discard(done)
            self._holders[key] -= 1
            if not self._holders[key]:
                self._event(key).set()

        task.add_done_callback(release)
        return task

    def _holding(self, resource: str) -> str | None:
        """The key whose pause holds ``resource`` now, if any."""
        for key in (ALL, resource):
            if self._holders.get(key):
                return key
        return None

    def held(self, resource: str) -> bool:
        """Whether a rate-limit pause holds ``resource`` now."""
        return self._holding(resource) is not None

    async def wait(self, resource: str) -> None:
        """Return once no pause holds ``resource``.

        The pauses are checked again after each wakeup: another may have
        begun before this waiter ran.
        """
        while (key := self._holding(resource)) is not None:
            await self._event(key).wait()

    def stop(self, key: str) -> None:
        """Mark ``key``, or every resource for ``ALL``, spent for the run."""
        self._stopped.add(key)

    def stopped(self, resource: str) -> bool:
        """Whether a terminal rate limit has stopped ``resource``."""
        return resource in self._stopped or ALL in self._stopped

    def close(self) -> None:
        """Cancel every pause still running, as the transport closes."""
        for task in list(self._pauses):
            task.cancel()
