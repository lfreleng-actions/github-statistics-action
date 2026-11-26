# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The ``collect`` command: sync the store for the resolved scope (design §4.2, §6.5).

Each target syncs into its own store under its own budget, and the
command prints a short table of what each did. A spent budget exits 0:
a cold start takes several runs. So does a run that reaches its time
limit, ``--max-minutes`` from the command's start, which the budget
treats as spent. ``--strict`` exits 2 once every target is done if any
repository was unreadable; a rejected token exits 3 at once, naming the
variable that holds it, never its value.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from time import monotonic

import httpx
import typer
from rich.console import Console
from rich.table import Table

from github_statistics.client import (
    AuthError,
    Budget,
    BudgetExhaustedError,
    ClientError,
    Transport,
)
from github_statistics.collect import CollectResult, collect
from github_statistics.config import (
    ConfigError,
    Resolution,
    Target,
    default_cache_dir,
    resolve_token,
)
from github_statistics.store import Store, store_path

from .common import append_summary, fail, write_outputs
from .options import SharedOptions, shared_options

STRICT_EXIT = 2
AUTH_EXIT = 3
TIME_LIMIT = "time limit reached"

_Table = tuple[str, list[str], list[list[str]]]


def _backoff_within(
    budget: Budget,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Callable[[float], Awaitable[None]]:
    """A transport's backoff ``sleep`` that ends at ``budget``'s deadline.

    No retry starts at or past the deadline, not even one of the
    uncharged ``GET /rate_limit`` seed, which the budget never refuses:
    a backoff that would end later waits until the deadline, so a
    rate-limit pause still holds until then, and raises
    ``BudgetExhaustedError``, as does one that wakes with time up.
    """

    async def backoff(delay: float) -> None:
        left = delay if budget.deadline is None else budget.deadline - budget.clock()
        await sleep(max(min(delay, left), 0))
        if left < delay or budget.timed_out:
            raise BudgetExhaustedError("time limit reached; not retrying")

    return backoff


async def _collect_target(
    resolution: Resolution,
    target: Target,
    *,
    now: datetime,
    until: date | None,
    refresh: bool,
    deadline: float | None,
) -> CollectResult:
    """Collect ``target`` with its own transport, budget and store.

    ``until`` ends the window, by default the day before ``now``.
    ``deadline``, a `monotonic` time, ends the reads like a spent
    budget. The step it stopped is marked `TIME_LIMIT`; when a count
    stopped an earlier step first, that step is named and the time
    limit noted as reached later.
    """
    config = resolution.config
    limits = config.budget
    budget = Budget(
        rest=limits.rest,
        graphql_points=limits.graphql_points,
        reserve=limits.reserve,
        deadline=deadline,
        clock=monotonic,
    )
    directory = config.cache.directory
    cache = Path(directory) if directory else default_cache_dir()
    async with Transport(
        resolve_token(target), budget=budget, sleep=_backoff_within(budget)
    ) as transport:
        # A rate limit outlasting the retries has stopped the transport:
        # collect then sends nothing and reports its scope step spent.
        # Past the deadline, as an earlier target may leave it, the seed
        # is skipped too, and collect only reports what is stored; one
        # whose retries reach the deadline stops the same way.
        if not budget.timed_out:
            with contextlib.suppress(BudgetExhaustedError):
                await transport.seed_budget()
        host = httpx.URL(transport.api_url).host
        with Store(store_path(cache, host, target.owner)) as store:
            result = await collect(
                config,
                target,
                now=now,
                transport=transport,
                store=store,
                identities=resolution.cli_identities,
                refresh=refresh,
                until=until,
            )
    if result.exhausted and budget.timed_out:
        # The step named is the first one stopped: the time limit's own,
        # unless a count stopped the run before the deadline did.
        if budget.stopped_by_count:
            exhausted = f"{result.exhausted}; {TIME_LIMIT} later"
        else:
            exhausted = f"{result.exhausted} ({TIME_LIMIT})"
        result = dataclasses.replace(result, exhausted=exhausted)
    return result


def collection_deadline(minutes: int | None) -> float | None:
    """The `monotonic` time ``minutes`` from now; ``None`` for no limit."""
    return None if minutes is None else monotonic() + minutes * 60


def _tables(results: Sequence[CollectResult]) -> list[_Table]:
    """The summary of ``results`` as titled tables of plain text."""
    runs = [
        [
            result.owner,
            str(result.requests),
            str(result.graphql_points),
            str(result.not_modified),
            result.exhausted or "no",
            str(result.gap_days),
        ]
        for result in results
    ]
    coverage = [
        [result.owner, metric, str(days.complete), str(days.partial), str(days.missing)]
        for result in results
        for metric, days in result.coverage.items()
    ]
    repositories = [
        [key, reason]
        for result in results
        for group in (result.unreadable, result.dropped)
        for key, reason in group.items()
    ]
    repositories += [
        [key, "runs read stalled"] for result in results for key in result.stalled
    ]
    tables: list[_Table] = [
        (
            "Collection",
            [
                "Owner",
                "Requests",
                "GraphQL points",
                "304s",
                "Budget spent in",
                "Gap days",
            ],
            runs,
        ),
        ("Coverage", ["Owner", "Metric", "Complete", "Partial", "Missing"], coverage),
    ]
    if repositories:
        tables.append(("Repositories", ["Repository", "Status"], repositories))
    return tables


def _markdown(tables: list[_Table]) -> str:
    """``tables`` as Markdown, for the job summary."""
    lines: list[str] = []
    for title, headers, rows in tables:
        lines += [f"### {title}", "", "| " + " | ".join(headers) + " |"]
        lines.append("|" + "|".join(" --- " for _ in headers) + "|")
        lines += ["| " + " | ".join(row) + " |" for row in rows]
        lines.append("")
    return "\n".join(lines)


def collect_target(
    resolution: Resolution,
    target: Target,
    *,
    now: datetime,
    until: date | None,
    refresh: bool,
    deadline: float | None = None,
) -> CollectResult:
    """Collect ``target``; a rejected token or a failed read raises the exit to raise.

    ``deadline``, from `collection_deadline`, bounds the reads. A
    rejected token exits `AUTH_EXIT`, naming the variable that holds it,
    never its value; any other client failure exits 1.
    """
    try:
        return asyncio.run(
            _collect_target(
                resolution,
                target,
                now=now,
                until=until,
                refresh=refresh,
                deadline=deadline,
            )
        )
    except AuthError:
        raise fail(
            f"GitHub rejected the token in ${target.token_env} (HTTP 401)", AUTH_EXIT
        ) from None
    except (ClientError, httpx.HTTPError) as exc:
        raise fail(f"cannot collect {target.owner}: {exc}") from None


def report(results: Sequence[CollectResult]) -> str:
    """Print the summary tables of ``results``; the same tables as Markdown."""
    tables = _tables(results)
    console = Console()
    for title, headers, rows in tables:
        table = Table(*headers, title=title)
        for row in rows:
            table.add_row(*row)
        console.print(table)
    return _markdown(tables)


def collect_outputs(results: Sequence[CollectResult]) -> dict[str, str]:
    """The step outputs ``results`` give: requests, coverage and gap days."""
    complete = all(result.coverage_complete for result in results)
    return {
        "requests_used": str(sum(result.requests for result in results)),
        "coverage_complete": str(complete).lower(),
        "gap_days": str(sum(result.gap_days for result in results)),
    }


@shared_options
def collect_command(
    options: SharedOptions,
    refresh: bool = typer.Option(
        False, "--refresh", help="Ignore watermarks and coverage; re-read the window."
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Exit 2 if any repository was unreadable."
    ),
) -> None:
    """Sync the store for each target's resolved public repositories."""
    try:
        resolution = options.resolve()
    except ConfigError as exc:
        raise fail(str(exc)) from None
    deadline = collection_deadline(resolution.config.budget.minutes)
    now = datetime.now(UTC)
    results = [
        collect_target(
            resolution,
            target,
            now=now,
            until=options.until,
            refresh=refresh,
            deadline=deadline,
        )
        for target in resolution.config.targets
    ]
    append_summary(report(results))
    write_outputs(collect_outputs(results))
    if strict and any(result.unreadable for result in results):
        raise fail("some repositories were unreadable (--strict)", STRICT_EXIT)
