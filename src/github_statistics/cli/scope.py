# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The ``scope`` command: print the resolved public repositories (design §4.2, §9.2).

Standard output carries repository names alone, so a workflow can hand
them to the token action; every warning and the dropped list go to
standard error. An empty scope prints nothing and exits 0: the caller
checks for empty output, since an empty repository list given to the
token action would grant the whole installation. A repository whose
read failed leaves the scope incomplete, so the command then prints no
names and exits 1. The command never touches the store.
"""

from __future__ import annotations

import asyncio
import json
from enum import StrEnum

import httpx
import typer

from github_statistics.client import Budget, ClientError, Transport
from github_statistics.config import ConfigError, resolve_token
from github_statistics.scope import ResolvedScope, resolve_scope

from .common import fail
from .options import SharedOptions, shared_options


class Format(StrEnum):
    """How ``scope`` prints the repository names."""

    LINES = "lines"
    CSV = "csv"
    JSON = "json"


def _render(names: list[str], output: Format) -> str:
    """``names`` in ``output`` format."""
    if output is Format.CSV:
        return ",".join(names)
    if output is Format.JSON:
        return json.dumps(names)
    return "\n".join(names)


@shared_options
def scope(
    options: SharedOptions,
    output: Format = typer.Option(
        Format.LINES, "--format", help="Output format for the repository names."
    ),
) -> None:
    """Print the public repositories one owner resolves to, one name per line."""
    try:
        config = options.resolve().config
    except ConfigError as exc:
        raise fail(str(exc)) from None
    if len(config.targets) != 1:
        raise fail(
            "scope resolves one owner at a time; select one with --org or --repo"
        )
    [target] = config.targets

    limits = config.budget
    budget = Budget(
        rest=limits.rest, graphql_points=limits.graphql_points, reserve=limits.reserve
    )

    async def run() -> ResolvedScope:
        async with Transport(resolve_token(target), budget=budget) as transport:
            await transport.seed_budget()
            return await resolve_scope(transport, target)

    try:
        resolved = asyncio.run(run())
    except (ClientError, httpx.HTTPError) as exc:
        raise fail(f"cannot resolve the scope of {target.owner}: {exc}") from None

    for drop in resolved.dropped:
        typer.echo(f"dropped {resolved.owner}/{drop.name}: {drop.reason}", err=True)
    for name in resolved.unresolved:
        typer.echo(f"warning: could not read {resolved.owner}/{name}", err=True)
    if resolved.unresolved:
        raise fail(
            f"the scope of {resolved.owner} is incomplete:"
            f" {len(resolved.unresolved)} repositories could not be read"
        )
    names = [repository.name for repository in resolved.repositories]
    if not names:
        typer.echo(
            f"warning: no public repositories in scope for {resolved.owner}", err=True
        )
        return
    typer.echo(_render(names, output))
