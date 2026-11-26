# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The ``doctor`` command: whether a setup is sound (design §4.2).

It composes what the other commands already compute and writes
nothing. Only an invalid configuration (exit 4) or a rejected token
(exit 3) fails it; a missing store, an empty scope and unclassified
bots are reported.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import typer

from github_statistics.client import (
    AuthError,
    Budget,
    BudgetExhaustedError,
    ClientError,
    Transport,
)
from github_statistics.collect import runs_horizon
from github_statistics.collect.run import window_coverage
from github_statistics.config import (
    Config,
    ConfigError,
    Resolution,
    Target,
    default_cache_dir,
    resolve_token,
)
from github_statistics.scope import resolve_scope
from github_statistics.store import store_path

from .cache import info as cache_info
from .collect import AUTH_EXIT
from .common import API_HOST, ReadOnlyStore, StaleStoreError, fail
from .config import _redact
from .options import SharedOptions, shared_options

CONFIG_EXIT = 4
IDENTITY_KEYS = ("org_bots", "external_bots", "members", "member_orgs", "ignore_logins")
BOT_KEYS = "identities.org_bots or identities.external_bots"


def _configuration(resolution: Resolution) -> None:
    """The winning source and each target's identity lists with their provenance."""
    where = f" ({resolution.location})" if resolution.location else ""
    typer.echo(f"configuration: {resolution.source}{where}")
    for target in resolution.config.targets:
        identities = resolution.identities_for(target)
        typer.echo(f"identities for {target.owner}:")
        for key in IDENTITY_KEYS:
            found = getattr(identities, key)
            values = ", ".join(_redact(list(found.values))) or "none"
            typer.echo(f"  {key}: {values} ({found.source})")


async def _network(target: Target, config: Config, known: list[str]) -> None:
    """Print the rate limit left and the resolved scope; `AuthError` propagates.

    The scope reads spend ``config``'s budget, seeded from the rate
    limit, so they keep the configured reserve, and are skipped when
    no count was observed; ``known`` are the repositories the store
    holds, so one turned private is reported.
    """
    limits = config.budget
    budget = Budget(
        rest=limits.rest, graphql_points=limits.graphql_points, reserve=limits.reserve
    )
    async with Transport(resolve_token(target), budget=budget) as transport:
        try:
            response = await transport.request("GET", "/rate_limit", charge=False)
            response.raise_for_status()
            core = response.json()["resources"]["core"]
            remaining = int(core["remaining"])
            budget.observe(remaining, core.get("reset"))
            left = f"{remaining} of {core['limit']} left"
        except AuthError:
            raise
        except (ClientError, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            left = f"unknown ({exc})"
        typer.echo(f"  rate limit: {left}")
        if budget.remaining is None:
            # Without a count the budget cannot hold the reserve back.
            typer.echo(f"scope of {target.owner}: skipped (no rate limit count)")
            return
        try:
            scope = await resolve_scope(transport, target, known=known)
        except AuthError:
            raise
        except BudgetExhaustedError as exc:
            typer.echo(f"scope of {target.owner}: budget spent ({exc})")
            return
        except (ClientError, httpx.HTTPError) as exc:
            typer.echo(f"scope of {target.owner}: cannot resolve ({exc})")
            return
    typer.echo(f"scope of {scope.owner}: {len(scope.repositories)} repositories")
    for repository in scope.repositories:
        typer.echo(f"  selected {scope.owner}/{repository.name}")
    for drop in scope.dropped:
        typer.echo(f"  dropped {scope.owner}/{drop.name}: {drop.reason}")
    for name in scope.unresolved:
        typer.echo(f"  could not read {scope.owner}/{name}")


def _stored(path: Path) -> list[str]:
    """The repositories the store at ``path`` holds; none when it cannot be read."""
    if not path.is_file():
        return []
    try:
        with ReadOnlyStore(path) as store:
            return store.stored_repositories()
    except (StaleStoreError, sqlite3.DatabaseError):
        return []


def _open(path: Path, owner: str) -> ReadOnlyStore | None:
    """``owner``'s store read-only, or ``None`` after saying why it cannot be read."""
    reason = f"no store for {owner} at {path}; run collect first"
    if path.is_file():
        try:
            return ReadOnlyStore(path)
        except StaleStoreError:
            reason = f"the store for {owner} at {path} is stale; run collect"
        except sqlite3.DatabaseError as exc:
            reason = f"cannot read the store for {owner} at {path}: {exc}"
    typer.echo(reason)
    return None


def _health(
    resolution: Resolution, target: Target, store: ReadOnlyStore, until: date
) -> None:
    """Coverage over the window, public members and unclassified bots."""
    # Imported here so other commands start without loading matplotlib.
    from github_statistics.series import unclassified_bots

    config = resolution.config
    start = until - timedelta(days=config.window.days - 1)
    metrics = config.metrics
    enabled = [f.name for f in dataclasses.fields(metrics) if getattr(metrics, f.name)]
    coverage, _gaps = window_coverage(store, target.owner, enabled, start, until)
    typer.echo(f"coverage of {target.owner}, {start} to {until}:")
    for metric, days in coverage.items():
        typer.echo(
            f"  {metric}: {days.complete} complete, {days.partial} partial,"
            f" {days.missing} missing"
        )
    horizon = runs_horizon(store, target.owner)
    typer.echo(f"  workflow runs horizon: {horizon or 'no runs held'}")
    identities = resolution.identities_for(target)
    typer.echo(f"public members of {target.owner}'s member organisations:")
    for org in identities.member_orgs.values:
        typer.echo(f"  {_redact(org)}: {len(store.member_logins([org]))}")
    bots = unclassified_bots(store, identities)
    typer.echo(f"unclassified bots of {target.owner}: {len(bots)}")
    for login in bots:
        typer.echo(f"  {login}: add to {BOT_KEYS}")


@shared_options
def doctor(
    options: SharedOptions,
    offline: bool = typer.Option(
        False, "--offline", help="Skip the rate limit and scope checks."
    ),
) -> None:
    """Report the configuration, token, scope, store, coverage and bots; no writes."""
    try:
        resolution = options.resolve()
    except ConfigError as exc:
        raise fail(str(exc), CONFIG_EXIT) from None
    config = resolution.config
    _configuration(resolution)
    cache = Path(config.cache.directory or default_cache_dir())
    for target in config.targets:
        # Shown redacted; the original still names the variable to read.
        variable = _redact(target.token_env)
        state = "is set" if resolve_token(target) else "is not set"
        typer.echo(f"token for {target.owner}: ${variable} {state}")
        if offline:
            typer.echo("  rate limit: skipped (--offline)")
            typer.echo(f"scope of {target.owner}: skipped (--offline)")
            continue
        try:
            known = _stored(store_path(cache, API_HOST, target.owner))
            asyncio.run(_network(target, config, known))
        except AuthError:
            message = f"GitHub rejected the token in ${variable} (HTTP 401)"
            raise fail(message, AUTH_EXIT) from None
    try:
        cache_info(cache_dir=str(cache), config=None, config_data=None)
    except sqlite3.DatabaseError as exc:
        # One unreadable store must not hide the per-target report below.
        typer.echo(f"cannot inspect the stores under {cache}: {exc}")
    until = options.until or datetime.now(UTC).date() - timedelta(days=1)
    for target in config.targets:
        path = store_path(cache, API_HOST, target.owner)
        store = _open(path, target.owner)
        if store is None:
            continue
        try:
            with store:
                _health(resolution, target, store, until)
        except sqlite3.DatabaseError as exc:
            typer.echo(f"cannot read the store for {target.owner} at {path}: {exc}")
