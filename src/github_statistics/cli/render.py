# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The ``render`` command: every target's charts from the store (design §4.2, §8.4).

Needs no token and sends no request. Each store opens read-only, so
rendering never creates, rebuilds or writes one; a missing or stale
store fails with a message naming ``collect``, and one SQLite cannot
read fails naming its path. A repository selection cannot narrow a
store, so a selection that drops a repository the store held in scope
during the window fails rather than chart that repository under the
selection's title. Standard output lists the files written, one per
line.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path

import typer

from github_statistics.config import ConfigError, Resolution, Target, default_cache_dir
from github_statistics.store import store_path

from .common import API_HOST, ReadOnlyStore, StaleStoreError, fail
from .options import SharedOptions, shared_options


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    """Whether ``name`` matches any of ``patterns``, ignoring case, as `collect` does."""
    return any(fnmatchcase(name.lower(), pattern.lower()) for pattern in patterns)


def _unselected(target: Target, repositories: list[str]) -> list[str]:
    """The ``owner/name`` keys in ``repositories`` that ``target``'s selection drops."""
    dropped = []
    for repo in repositories:
        name = repo.partition("/")[2]
        if (target.include and not _matches(name, target.include)) or _matches(
            name, target.exclude
        ):
            dropped.append(repo)
    return dropped


def render_targets(
    resolution: Resolution,
    targets: Sequence[Target],
    *,
    until: date,
    now: datetime,
) -> Iterator[Path]:
    """Render the charts of ``targets``; yield each file as it is written.

    ``until`` ends the window and ``now`` dates the footers. Every store
    is read and checked before the first chart is drawn, so a failure
    there, raised as the exit `fail` builds, leaves no partial output.
    """
    # Imported here so other commands start without loading matplotlib.
    from github_statistics.render.charts import render_target
    from github_statistics.series import build_series

    config = resolution.config
    root = Path(config.cache.directory or default_cache_dir())
    stores = {
        target.owner: store_path(root, API_HOST, target.owner) for target in targets
    }
    for owner, path in stores.items():
        if not path.is_file():
            raise fail(f"no store for {owner} at {path}: run `collect` first")
    built = []
    for target in targets:
        path = stores[target.owner]
        try:
            with ReadOnlyStore(path) as store:
                series = build_series(
                    store,
                    config,
                    target,
                    until=until,
                    identities=resolution.identities_for(target),
                )
                dropped = _unselected(
                    target, store.scoped_between(series.days[0], series.days[-1])
                )
        except StaleStoreError:
            raise fail(
                f"the store at {path} has another schema version: run `collect` first"
            ) from None
        except sqlite3.DatabaseError as exc:
            raise fail(f"cannot read the store at {path}: {exc}") from None
        if dropped:
            raise fail(
                f"the store for {target.owner} holds {', '.join(dropped)} in the"
                " window, outside the repository selection, and render cannot"
                " leave it out: render without the selection, or `collect` the"
                " selection into its own --cache-dir"
            )
        built.append((target, series))
    for target, series in built:
        try:
            charts = render_target(
                series, config, target, Path(config.output.directory), now=now
            )
        except ValueError as exc:
            raise fail(str(exc)) from None
        for chart in charts:
            yield from chart.paths


@shared_options
def render(options: SharedOptions) -> None:
    """Render the charts of every target from the store; needs no token."""
    try:
        resolution = options.resolve()
    except ConfigError as exc:
        raise fail(str(exc)) from None
    now = datetime.now(UTC)
    until = options.until or now.date() - timedelta(days=1)
    for written in render_targets(
        resolution, resolution.config.targets, until=until, now=now
    ):
        typer.echo(str(written))
