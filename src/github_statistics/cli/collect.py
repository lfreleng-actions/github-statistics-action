# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The ``collect`` command: sync the store for the resolved scope (design §4.2, §6.5).

Each target syncs into its own store under its own budget, and the
command prints a short table of what each did. A spent budget exits 0:
a cold start takes several runs. ``--strict`` exits 2 once every target
is done if any repository was unreadable; a rejected token exits 3 at
once, naming the variable that holds it, never its value.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

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

from .options import SharedOptions, shared_options

STRICT_EXIT = 2
AUTH_EXIT = 3

_Table = tuple[str, list[str], list[list[str]]]


async def _collect_target(
    resolution: Resolution,
    target: Target,
    *,
    now: datetime,
    until: date | None,
    refresh: bool,
) -> CollectResult:
    """Collect ``target`` with its own transport, budget and store.

    ``until`` ends the window, by default the day before ``now``.
    """
    config = resolution.config
    limits = config.budget
    budget = Budget(
        rest=limits.rest, graphql_points=limits.graphql_points, reserve=limits.reserve
    )
    directory = config.cache.directory
    cache = Path(directory) if directory else default_cache_dir()
    async with Transport(resolve_token(target), budget=budget) as transport:
        # A rate limit outlasting the retries has stopped the transport:
        # collect then sends nothing and reports its scope step spent.
        with contextlib.suppress(BudgetExhaustedError):
            await transport.seed_budget()
        host = httpx.URL(transport.api_url).host
        with Store(store_path(cache, host, target.owner)) as store:
            return await collect(
                config,
                target,
                now=now,
                transport=transport,
                store=store,
                identities=resolution.cli_identities,
                refresh=refresh,
                until=until,
            )


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


def _report(results: Sequence[CollectResult]) -> None:
    """Print the tables; append them and the outputs to the GitHub files set."""
    tables = _tables(results)
    console = Console()
    for title, headers, rows in tables:
        table = Table(*headers, title=title)
        for row in rows:
            table.add_row(*row)
        console.print(table)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(_markdown(tables))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        complete = all(result.coverage_complete for result in results)
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"requests_used={sum(r.requests for r in results)}\n")
            handle.write(f"coverage_complete={str(complete).lower()}\n")
            handle.write(f"gap_days={sum(r.gap_days for r in results)}\n")


def _fail(message: str, code: int = 1) -> typer.Exit:
    """Report ``message`` on standard error; the exit to raise."""
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code)


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
        raise _fail(str(exc)) from None
    now = datetime.now(UTC)
    results: list[CollectResult] = []
    for target in resolution.config.targets:
        try:
            result = asyncio.run(
                _collect_target(
                    resolution, target, now=now, until=options.until, refresh=refresh
                )
            )
        except AuthError:
            raise _fail(
                f"GitHub rejected the token in ${target.token_env} (HTTP 401)",
                AUTH_EXIT,
            ) from None
        except (ClientError, httpx.HTTPError) as exc:
            raise _fail(f"cannot collect {target.owner}: {exc}") from None
        results.append(result)
    _report(results)
    if strict and any(result.unreadable for result in results):
        raise _fail("some repositories were unreadable (--strict)", STRICT_EXIT)
