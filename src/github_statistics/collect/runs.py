# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Collect workflow runs from the Actions listing (design §6.4).

The ``runs`` sync state holds the newest ``created_at`` stored as
``watermark``, the listing's first-page ETag, and ``oldest``: every run
from ``oldest`` to the watermark is stored, unless the backfill reached
the listing horizon. A day is covered once every run on it is read.
The ``runs_gap`` state's ``watermark`` holds the end of the newest
range read only up to the listing cap; no later read covers its day.
Its ``oldest`` holds the start of the oldest such range; the backfill
covers no day from that one up.
The ``runs_hole`` state holds a range an incremental read left unread
below the runs it stored, from ``oldest`` to ``watermark``; the next
read fills it first, and covers its days only once it is filled; the
state is cleared once they are covered.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx

from github_statistics.client import (
    BudgetExhaustedError,
    Transport,
    get_conditional,
    get_paged,
)
from github_statistics.client.policy import rate_limited
from github_statistics.scope import ResolvedScope
from github_statistics.store import Run, Store, SyncState

from .common import run_concurrently, scope_keys, unreadable_status
from .listed import listed_run, reaches
from .reader import PER_PAGE, RUNS_KEY, SliceReader
from .slices import DAY, SECOND, first_full_day, midnight, slice_below, timestamp

log = logging.getLogger(__name__)

# The sync state and coverage metric of the listing.
RUNS_METRIC = "runs"
HOLE_METRIC = "runs_hole"
PENDING_LIMIT = 50
SLICE_DAYS = 7


@dataclass(frozen=True)
class RunSyncResult:
    """What one repository's run sync did.

    ``current``: the incremental read finished. ``reached``: history
    reaches the window start or the listing horizon. ``unreadable``: the
    HTTP status that made the repository unreadable, else ``None``.
    ``stalled``: a spent budget stopped a read inside the second it
    started from (`SliceReader.read`).
    """

    repo: str
    runs: int
    pages: int
    current: bool
    reached: bool
    unreadable: int | None = None
    stalled: bool = False


class _RunSync(SliceReader):
    """One repository's run sync, in the phases `sync_all_runs` orders."""

    def __init__(
        self,
        transport: Transport,
        store: Store,
        key: str,
        *,
        window_start: date,
        now: datetime,
    ) -> None:
        """Prepare the sync of ``key`` (``owner/name``) from its stored state."""
        super().__init__(transport, store, key)
        self.window_start = window_start
        self.target = midnight(window_start)
        self.now = now
        self.yesterday = now.astimezone(UTC).date() - DAY
        self.state = store.sync_state(key, RUNS_METRIC) or SyncState(key, RUNS_METRIC)
        self.hole = store.sync_state(key, HOLE_METRIC)
        self.current = False
        self.unreadable: int | None = None

    def result(self) -> RunSyncResult:
        """What the sync has done so far."""
        oldest = self.state.oldest
        reached = oldest is not None and oldest <= self.target
        return RunSyncResult(
            self.key,
            self.runs,
            self.pages,
            self.current,
            reached,
            self.unreadable,
            self.stalled,
        )

    async def guarded(self, phase: Callable[[], Awaitable[None]]) -> bool:
        """Run ``phase``; whether it finished.

        A spent budget or rate limit stops it cleanly; an unreadable
        status (`unreadable_status`) marks the repository unreadable,
        keeping what was read.
        """
        try:
            await phase()
        except BudgetExhaustedError as exc:
            log.info("stopped syncing runs of %s: %s", self.key, exc)
            return False
        except httpx.HTTPStatusError as exc:
            status = unreadable_status(exc)
            if status is None:
                log.info("stopped syncing runs of %s: rate limited", self.key)
            else:
                log.warning("cannot read runs of %s: HTTP %s", self.key, status)
                self.unreadable = status
            return False
        return True

    async def recent(self) -> None:
        """The incremental read, then the pending re-reads.

        The read's coverage is added once the re-reads end, unless one
        finds the repository unreadable: the sync state and hole then go
        back to what they were, so the next sync reads those runs again
        and covers their days.
        """
        pending = {run.id for run in self.store.pending_runs(self.key)}
        before, hole = self.state, self.hole
        span = await self._incremental()
        self.current = True
        unreadable = False
        try:
            await self._reread(pending)
        except httpx.HTTPStatusError as exc:
            unreadable = not rate_limited(exc.response)
            raise
        finally:
            if unreadable:
                self._save(before)
                self._hole(hole or SyncState(self.key, HOLE_METRIC))
            elif span is not None:
                self._cover(*span)
                if self.hole is not None and self.hole.oldest is not None:
                    self._hole(SyncState(self.key, HOLE_METRIC))

    async def backfill(self) -> None:
        """Read week slices below ``oldest``, newest first, to the window start.

        An empty slice is followed by one probe for the newest run listed
        below it (`_older`). The days between are empty: they gain
        coverage, and the backfill goes on from the slice ending on that
        run's day. With nothing older listed, the backfill ends at the
        listing horizon, covering none of the slice's days but that of an
        intra-day ``oldest`` it completes. A split slice checkpoints after
        each part (`_checkpoint`).
        """
        cursor = self.state.oldest
        while cursor is not None and cursor > self.target:
            low = slice_below(cursor, self.target, SLICE_DAYS)
            read, complete = await self.read(low, cursor - SECOND, self._checkpoint)
            if read == 0:
                older = await self._older(low)
                if older is None:
                    log.info("no runs of %s listed before %s", self.key, low)
                    self._cover_read(cursor.astimezone(UTC).date())
                    self._save(dataclasses.replace(self.state, oldest=self.target))
                    return
                after = midnight(older.astimezone(UTC).date()) + DAY
                low = max(self.target, min(low, after))
            self._checkpoint(low, complete)
            cursor = low

    async def _older(self, below: datetime) -> datetime | None:
        """When the newest run listed as created before ``below`` was, if any."""
        params = {"per_page": 1, "created": f"<{timestamp(below)}"}
        response = await self.transport.request("GET", self.path, params=params)
        self.pages += 1
        response.raise_for_status()
        listed = response.json()[RUNS_KEY]
        if not listed:
            return None
        return datetime.fromisoformat(listed[0]["created_at"])

    def _checkpoint(self, low: datetime, complete: bool) -> None:
        """Record that every run from ``low`` up to ``oldest`` is read.

        If ``complete``, cover the whole days read, short of the day of a
        part read only up to the cap (`_cover_read`).
        """
        top = self.state.oldest
        if top is None or low >= top:
            return
        if complete:
            self._cover_read(first_full_day(low))
        self._save(dataclasses.replace(self.state, oldest=low))

    def _cover_read(self, start: date) -> None:
        """Cover the days from ``start`` to the last one ending above ``oldest``.

        Every run from ``oldest`` up is read, so a caller that has read
        all of ``start``'s day and the days after it below ``oldest``
        completes them. Stops short of the day of the oldest part read
        only up to the cap (the ``runs_gap`` state).
        """
        top = self.state.oldest
        if top is None:
            return
        end = first_full_day(top) - DAY
        if self.gap_low is not None:
            end = min(end, self.gap_low.astimezone(UTC).date() - DAY)
        self._cover(start, end)

    async def _incremental(self) -> tuple[date, date] | None:
        """Read the listing newest first down to the watermark; days to cover.

        A hole an earlier read left is filled first (`_fill`). Where the
        listing stops at its cap, or a spent budget stops the paging,
        above the watermark, the runs read are stored and the rest
        becomes a hole, which date slices fill; on a first sync, the
        backfill reads it instead.
        """
        start = await self._fill()
        state = self.state
        since = state.watermark or state.oldest or self.target
        start = since if start is None else min(start, since)
        answer = await get_conditional(
            self.transport, self.path, {"per_page": PER_PAGE}, state.etag
        )
        self.pages += 1
        if answer.not_modified:
            self._store(dataclasses.replace(state, last_sync_at=self.now), [])
            return self._span(start)
        total = int(answer.body["total_count"])
        listed = [listed_run(self.key, data) for data in answer.body[RUNS_KEY]]
        stopped: BudgetExhaustedError | None = None
        link = answer.response.links.get("next")
        if link and link.get("url") and not reaches(listed, since):
            url = str(answer.response.url.join(link["url"]))
            try:
                async for page, _response in get_paged(
                    self.transport, url, items_key=RUNS_KEY
                ):
                    self.pages += 1
                    batch = [listed_run(self.key, data) for data in page]
                    listed.extend(batch)
                    if reaches(batch, since):
                        break
            except BudgetExhaustedError as exc:
                log.info("budget spent paging runs of %s; next sync resumes", self.key)
                stopped = exc
        oldest = state.oldest or self.target
        hole = None
        if listed and not reaches(listed, since) and (stopped or len(listed) < total):
            bottom = min(run.created_at for run in listed)
            if state.oldest is None:
                oldest = bottom + SECOND
            else:
                hole = SyncState(self.key, HOLE_METRIC, watermark=bottom, oldest=start)
                self._hole(hole)
        newest = [run.created_at for run in listed]
        if state.watermark is not None:
            newest.append(state.watermark)
        new = dataclasses.replace(
            state,
            watermark=max(newest, default=None),
            etag=answer.etag,
            oldest=oldest,
            last_sync_at=self.now,
        )
        self._store(new, listed)
        if hole is not None:
            if stopped is not None:
                raise stopped
            await self._fill()
        return self._span(start)

    async def _fill(self) -> datetime | None:
        """Read the stored hole, newest first; where its days start, if any.

        Each page and part read lowers the hole's top
        (`_fill_checkpoint`), so a spent budget resumes below it. A filled
        hole keeps its start, with its top below it, until `recent`
        covers its days.
        """
        hole = self.hole
        if hole is None or hole.oldest is None or hole.watermark is None:
            return None
        if hole.watermark >= hole.oldest:
            await self.read(hole.oldest, hole.watermark, self._fill_checkpoint)
            self._hole(dataclasses.replace(hole, watermark=hole.oldest - SECOND))
        return hole.oldest

    def _fill_checkpoint(self, low: datetime, _complete: bool) -> None:
        """Record that the hole's runs from ``low`` up are read."""
        hole = self.hole
        if hole is not None and hole.watermark is not None:
            if low - SECOND < hole.watermark:
                self._hole(dataclasses.replace(hole, watermark=low - SECOND))

    def _store(self, state: SyncState, listed: list[Run]) -> None:
        """Store the runs an incremental read listed, with its sync state."""
        self.store.store_run_page(listed, state)
        self.state = state
        self.runs += len(listed)

    def _span(self, since: datetime) -> tuple[date, date] | None:
        """The days a finished incremental read covers.

        They run from that of ``since`` to yesterday, after the day of
        the newest range read only up to the cap (the ``runs_gap`` state).
        """
        state = self.state
        if state.oldest is None:
            return None
        start = max(since.astimezone(UTC).date(), first_full_day(state.oldest))
        if self.gap is not None:
            start = max(start, self.gap.astimezone(UTC).date() + DAY)
        return start, self.yesterday

    async def _reread(self, pending: set[int]) -> None:
        """Re-read by id, oldest first, up to `PENDING_LIMIT` of ``pending``."""
        due = [run for run in self.store.pending_runs(self.key) if run.id in pending]
        for run in due[:PENDING_LIMIT]:
            response = await self.transport.request("GET", f"{self.path}/{run.id}")
            self.pages += 1
            response.raise_for_status()
            self.store.upsert_runs([listed_run(self.key, response.json())])
            self.runs += 1

    def _cover(self, start: date, end: date) -> None:
        """Cover ``start`` to ``end``, clipped to the window and to yesterday."""
        start = max(start, self.window_start)
        end = min(end, self.yesterday)
        if start <= end:
            self.store.add_coverage(self.key, RUNS_METRIC, start, end)

    def _hole(self, state: SyncState) -> None:
        """Store ``state`` as the hole the incremental read left."""
        self.store.set_sync_state(state)
        self.hole = state

    def _save(self, state: SyncState) -> None:
        """Store ``state`` as this repository's run sync state."""
        self.store.set_sync_state(state)
        self.state = state


async def sync_runs(
    transport: Transport,
    store: Store,
    owner: str,
    repo: str,
    *,
    window_start: date,
    now: datetime,
) -> RunSyncResult:
    """Sync the workflow runs of ``owner/repo`` into ``store``.

    The incremental read, up to `PENDING_LIMIT` re-reads of runs still
    in progress, then the backfill to the window start or the listing
    horizon. The budget is the transport's (design §6.5); a spent one
    stops cleanly, keeping what was read. An unreadable repository
    changes no coverage; one with Actions disabled covers the window.
    """
    sync = _RunSync(
        transport, store, f"{owner}/{repo}", window_start=window_start, now=now
    )
    if await sync.guarded(sync.recent):
        await sync.guarded(sync.backfill)
    return sync.result()


async def sync_all_runs(
    transport: Transport,
    store: Store,
    scope: ResolvedScope,
    *,
    window_start: date,
    now: datetime,
) -> dict[str, RunSyncResult]:
    """Sync the runs of every repository in ``scope``; results by ``owner/name``.

    Every incremental read finishes before any backfill starts, so the
    newest days stay current (design §6.5); a repository the first round
    did not finish is not backfilled. A stopping error propagates as
    itself, ``AuthError`` first, and cancels the others.
    """
    syncs = [
        _RunSync(transport, store, key, window_start=window_start, now=now)
        for key in scope_keys(store, scope, now)
    ]
    finished = await run_concurrently(sync.guarded(sync.recent) for sync in syncs)
    await run_concurrently(
        sync.guarded(sync.backfill)
        for sync, done in zip(syncs, finished, strict=True)
        if done
    )
    return {sync.key: sync.result() for sync in syncs}


def runs_horizon(store: Store, owner: str) -> date | None:
    """The listing horizon of ``owner``: the day of the oldest run held.

    GitHub lists runs back about 354 days (measured 2026-11-16); days
    before the horizon chart as no data. ``None`` when no run is held.
    """
    return store.earliest_run_day(owner)
