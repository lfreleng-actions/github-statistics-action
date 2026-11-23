# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Sweep workflow runs for re-runs (design §6.4).

A re-run keeps its ``created_at`` and raises ``run_attempt``, so the
incremental read never sees one older than the listing's first page.
GitHub allows a re-run for `RERUN_DAYS` after the first run, so once a
week the sweep re-reads every run created since then (`_RunSync.sweep`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from github_statistics.client import Transport
from github_statistics.store import Store

from .runs import SWEEP_EVERY, _RunSync


@dataclass(frozen=True)
class SweepResult:
    """What one repository's re-run sweep did.

    ``due``: a sweep was due, so one started. ``swept``: it ended, with no
    spent budget or unreadable repository, and moved ``swept_to``.
    ``unreadable`` and ``stalled`` are as on `RunSyncResult`.
    """

    repo: str
    runs: int
    pages: int
    due: bool
    swept: bool
    unreadable: int | None = None
    stalled: bool = False


async def sweep_runs(
    transport: Transport,
    store: Store,
    owner: str,
    repo: str,
    *,
    now: datetime,
) -> SweepResult:
    """Sweep the runs of ``owner/repo`` for re-runs, if a sweep is due.

    A sweep is due when ``swept_to`` is unset or more than `SWEEP_EVERY`
    before ``now``. It reads every run created from `RERUN_DAYS` before
    ``swept_to``, or with none the last `RERUN_DAYS` and `SWEEP_EVERY`,
    to ``now``. Run it after `sync_runs` for the repository. One cut
    short by a spent budget or an unreadable repository leaves
    ``swept_to`` unchanged, so the next run sweeps again from the same
    start. A first sweep has no ``swept_to`` to hold its start, so each
    retry starts from its own ``now``: a run the cut-short attempt did
    not reach that falls below the retry's start goes unswept. An hour
    over the listing cap is read up to the cap and still moves
    ``swept_to``, as in the backfill: no later sweep could read more.
    """
    # The sweep reads its own window; the sync window start goes unused.
    day = now.astimezone(UTC).date()
    sync = _RunSync(transport, store, f"{owner}/{repo}", window_start=day, now=now)
    swept_to = sync.state.swept_to
    due = swept_to is None or swept_to < now - SWEEP_EVERY
    swept = due and await sync.guarded(sync.sweep)
    return SweepResult(
        sync.key, sync.runs, sync.pages, due, swept, sync.unreadable, sync.stalled
    )
