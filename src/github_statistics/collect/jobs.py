# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Count the jobs of each concluded workflow run (design §6.4).

One GraphQL ``nodes(ids:)`` query reads fifty runs and the check runs
of each run's latest attempt (``checkType: LATEST``), what the run's
page shows. The counts of a batch are stored in one transaction, and
the days whose runs all hold a count regain jobs coverage.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from github_statistics.client import (
    BudgetExhaustedError,
    GraphQLError,
    Transport,
    TransportError,
    batched,
    graphql,
)
from github_statistics.store import Store

from .enrich_items import BATCH_COST, BATCH_SIZE, PAGE_COST, _not_found

log = logging.getLogger(__name__)

# More runs than a store holds, so a run sees every one needing a count.
ALL_RUNS = 100_000_000

JOBS_QUERY = """
query($ids: [ID!]!) {
  rateLimit { cost remaining }
  nodes(ids: $ids) {
    ... on WorkflowRun {
      id
      databaseId
      checkSuite { checkRuns(filterBy: {checkType: LATEST}) { totalCount } }
    }
  }
}
"""


@dataclass(frozen=True)
class JobsResult:
    """What one job-count run did.

    ``runs`` counts the runs given a job count. ``failed`` holds the
    node ids that failed alone and ``remaining`` those not reached; both
    are read again on the next run.
    """

    runs: int
    failed: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()


class _Stopped(BudgetExhaustedError):
    """Ends the batching early, so ``batched`` returns the ids not reached."""


def _forbidden(exc: GraphQLError) -> bool:
    """Whether GitHub refused to read a check suite: the token lacks ``checks``."""
    return any(
        isinstance(error, dict)
        and error.get("type") == "FORBIDDEN"
        and "checkSuite" in (error.get("path") or [])
        for error in exc.errors
    )


def _count(node: Any) -> int:
    """The job count of ``node``; ``0`` for a deleted run or one without a suite."""
    suite = node.get("checkSuite") if isinstance(node, dict) else None
    if not isinstance(suite, dict):
        return 0
    total = (suite.get("checkRuns") or {}).get("totalCount")
    return total if isinstance(total, int) else 0


async def _read(transport: Transport, ids: list[str]) -> list[Any]:
    """The nodes of ``ids``, in order; ``[None]`` for one id GitHub cannot find."""
    estimate = BATCH_COST if len(ids) > 1 else PAGE_COST
    try:
        data = await graphql(
            transport, JOBS_QUERY, {"ids": ids}, cost_estimate=estimate
        )
    except GraphQLError as exc:
        if _forbidden(exc):
            log.warning(
                "GitHub refused to read check suites: the token needs"
                " checks: read; job counts are left for a later run"
            )
            raise _Stopped("checks: read is missing") from exc
        if len(ids) == 1 and _not_found(exc, ["nodes", 0]):
            return [None]
        raise
    except TransportError as exc:
        log.warning(
            "stopped counting jobs at %s: %s; it and those after it"
            " are read again next run",
            ids[0],
            exc,
        )
        raise _Stopped(str(exc)) from exc
    return list(data.get("nodes") or [None] * len(ids))


async def enrich_jobs(
    transport: Transport, store: Store, repos: Iterable[str]
) -> JobsResult:
    """Store the job count of every concluded run of ``repos`` that has none.

    ``repos`` are the store's ``owner/name`` keys of the resolved scope;
    runs kept for a repository out of scope are not read, since the
    token may no longer reach them, and keep their stored history.
    Runs with a conclusion and no job count go fifty to a query, the
    newest first, so recent days complete first and a cold start fills
    in backwards. A run still in progress waits until the runs collector
    sees it conclude. The count is the check runs of the run's latest
    attempt; a run GitHub no longer finds, or one without a check suite,
    stores ``0`` so it is not read again. The store clears a run's count
    when its status, conclusion or attempt changes, so a re-run is
    counted again here. Each repository's jobs coverage then becomes
    its runs coverage less the days holding a run without a count.

    There is no ``budget`` argument: the budget is the one on
    ``transport`` (design §6.5), charged the cost each query reports.
    When fewer points are left than a batch may cost, one run is read
    alone; when the budget runs out, counting stops cleanly with the
    ids it did not reach in ``remaining``, and a run that completes
    nothing logs a warning naming ``budget.graphql_points``. A batch
    that fails is halved; an id that fails alone is logged and left in
    ``failed`` for the next run. A ``FORBIDDEN`` check suite, a token
    without ``checks: read``, logs one warning and ends the step, as
    does a transport error; the counts stored so far are kept, and no
    budget warning follows even when the refused query took the last
    point.
    """
    wanted = set(repos)
    runs = [
        run
        for run in store.runs_needing_jobs(ALL_RUNS)
        if run.conclusion and run.repo in wanted
    ]
    runs.sort(key=lambda run: (run.created_at, run.id), reverse=True)
    ids = {run.node_id: run.id for run in runs}
    done: set[str] = set()
    stopped = False

    async def fetch(chunk: list[str]) -> None:
        """Read one batch and store its counts."""
        nonlocal stopped
        # A last point cannot cover a batch but reads one run.
        budget = transport.budget
        last = (
            len(chunk) > 1 and budget is not None and budget.graphql_left < BATCH_COST
        )
        if last:
            chunk = chunk[:1]
        try:
            nodes = await _read(transport, chunk)
        except _Stopped:
            stopped = True
            raise
        counts = {ids[n]: _count(node) for n, node in zip(chunk, nodes, strict=True)}
        store.set_job_counts(counts)
        done.update(chunk)
        if last:
            raise BudgetExhaustedError("one GraphQL point left; read one run")

    def describe(chunk: list[str]) -> str:
        """Name a batch in the log."""
        return f"{len(chunk)} runs from {chunk[0]}"

    out = await batched(list(ids), BATCH_SIZE, fetch, describe)
    for node_id in out.failed:
        log.warning("could not count the jobs of run %d (%s)", ids[node_id], node_id)
    store.restore_jobs_coverage()
    remaining = tuple(node_id for node_id in out.remaining if node_id not in done)
    budget = transport.budget
    # A step ended by _read has logged its own cause; more points cannot help.
    if remaining and not done and not stopped and budget and budget.graphql_exhausted:
        log.warning(
            "the GraphQL budget was too small to count the jobs of any run;"
            " %d wait for the next run: raise budget.graphql_points",
            len(remaining),
        )
    return JobsResult(runs=len(done), failed=tuple(out.failed), remaining=remaining)
