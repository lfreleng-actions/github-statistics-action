# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Read a repository's runs in ``created=A..B`` date slices (design §6.4).

The run sync's incremental read and backfill read through it, and the
weekly sweep (#51) can too. The ``runs_gap`` sync state's ``watermark``
holds the end of the newest range read only up to the listing cap, and
its ``oldest`` the start of the oldest such range.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from urllib.parse import quote

from github_statistics.client import BudgetExhaustedError, Transport, get_paged
from github_statistics.store import Store, SyncState

from .listed import listed_run
from .slices import SECOND, split, timestamp

log = logging.getLogger(__name__)

GAP_METRIC = "runs_gap"
LISTING_CAP = 1000
PER_PAGE = 100
RUNS_KEY = "workflow_runs"
Checkpoint = Callable[[datetime, bool], None]


class SliceReader:
    """Reads the runs of one repository by date slice, counting what it reads."""

    def __init__(self, transport: Transport, store: Store, key: str) -> None:
        """Prepare to read the runs of ``key`` (``owner/name``)."""
        owner, _, name = key.partition("/")
        parts = ("repos", owner, name, "actions", "runs")
        self.path = "/" + "/".join(quote(part, safe="") for part in parts)
        self.transport = transport
        self.store = store
        self.key = key
        gap = store.sync_state(key, GAP_METRIC)
        self.gap = None if gap is None else gap.watermark
        self.gap_low = None if gap is None else gap.oldest
        self.runs = 0
        self.pages = 0

    async def read(
        self, low: datetime, high: datetime, checkpoint: Checkpoint
    ) -> tuple[int, bool]:
        """Store every run created from ``low`` to ``high``; (count, complete).

        A range over the listing cap is read in `split` parts, newest
        first; an hour still over it is read up to the cap and is
        incomplete. ``checkpoint`` gets each part once read and, when a
        spent budget stops the paging, the second above the oldest run
        read: runs sharing that second may lie on the unread page.
        """
        created = f"{timestamp(low)}..{timestamp(high)}"
        params = {"per_page": PER_PAGE, "created": created}
        read = 0
        first = True
        capped = False
        bottom: datetime | None = None
        pieces: list[tuple[datetime, datetime]] = []
        paged = get_paged(self.transport, self.path, params, items_key=RUNS_KEY)
        try:
            async for page, response in paged:
                self.pages += 1
                if first:
                    first = False
                    if int(response.json()["total_count"]) > LISTING_CAP:
                        pieces = split(low, high)
                        if pieces:
                            break
                        log.warning("over %d runs of %s", LISTING_CAP, self.key)
                        capped = True
                        self._mark_gap(low, high)
                runs = [listed_run(self.key, data) for data in page]
                self.store.upsert_runs(runs)
                read += len(runs)
                self.runs += len(runs)
                bottom = min((run.created_at for run in runs), default=bottom)
        except BudgetExhaustedError:
            if bottom is not None:
                checkpoint(bottom + SECOND, True)
            raise
        if not pieces:
            return read, not capped
        complete = True
        for start, end in reversed(pieces):
            count, done = await self.read(start, end, checkpoint)
            read += count
            complete = complete and done
            checkpoint(start, done)
        return read, complete

    def _mark_gap(self, low: datetime, high: datetime) -> None:
        """Record that runs created from ``low`` to ``high`` were not all read."""
        self.gap = high if self.gap is None else max(self.gap, high)
        self.gap_low = low if self.gap_low is None else min(self.gap_low, low)
        state = SyncState(self.key, GAP_METRIC, watermark=self.gap, oldest=self.gap_low)
        self.store.set_sync_state(state)
