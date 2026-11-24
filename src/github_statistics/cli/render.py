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
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.parse import urlsplit

import typer

from github_statistics.client.transport import DEFAULT_API_URL
from github_statistics.config import ConfigError, Target, default_cache_dir
from github_statistics.store import SCHEMA_VERSION, Store, store_path

from .options import SharedOptions, shared_options

API_HOST = urlsplit(DEFAULT_API_URL).hostname or "api.github.com"


class ReadOnlyStore(Store):
    """A `Store` over a read-only connection to an existing file.

    Unlike `Store`, opening it never creates the file or its directory
    and never rebuilds a schema; any write raises `sqlite3.OperationalError`.
    """

    def __init__(self, path: Path) -> None:
        """Open the store at ``path`` read-only."""
        self.path = Path(path)
        self._savepoints = 0
        uri = self.path.resolve().as_uri() + "?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        self._connection.row_factory = sqlite3.Row

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """Run the block's reads in one transaction, so all see one state of the file.

        The connection is in autocommit mode, so without this a collection
        committing between two queries could pair new counts with old coverage.
        """
        with self._transaction():
            yield

    def scoped_between(self, first: date, last: date) -> list[str]:
        """The repositories in scope on any day from ``first`` to ``last``, sorted.

        An interval that opened and closed on one day holds no day, so it
        does not count.
        """
        rows = self._connection.execute(
            "SELECT DISTINCT repo FROM scope WHERE from_day <= ?"
            " AND (to_day IS NULL OR (to_day > ? AND to_day > from_day))"
            " ORDER BY repo",
            (last.isoformat(), first.isoformat()),
        )
        return [row[0] for row in rows]


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


def _fail(message: str) -> typer.Exit:
    """Report ``message`` on standard error; the exit to raise."""
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(1)


@shared_options
def render(options: SharedOptions) -> None:
    """Render the charts of every target from the store; needs no token."""
    # Imported here so other commands start without loading matplotlib.
    from github_statistics.render.charts import render_target
    from github_statistics.series import build_series

    try:
        resolution = options.resolve()
    except ConfigError as exc:
        raise _fail(str(exc)) from None
    config = resolution.config
    now = datetime.now(UTC)
    until = options.until or now.date() - timedelta(days=1)
    root = Path(config.cache.directory or default_cache_dir())
    stores = {
        target.owner: store_path(root, API_HOST, target.owner)
        for target in config.targets
    }
    for owner, path in stores.items():
        if not path.is_file():
            raise _fail(f"no store for {owner} at {path}: run `collect` first")
    # Every store is read and checked before any chart is drawn, so a
    # failure leaves no partial output.
    built = []
    for target in config.targets:
        path = stores[target.owner]
        try:
            with ReadOnlyStore(path) as store, store.snapshot():
                if store.schema_version() != SCHEMA_VERSION:
                    raise _fail(
                        f"the store at {path} has another schema version:"
                        " run `collect` first"
                    )
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
        except sqlite3.DatabaseError as exc:
            raise _fail(f"cannot read the store at {path}: {exc}") from None
        if dropped:
            raise _fail(
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
            raise _fail(str(exc)) from None
        for chart in charts:
            for written in chart.paths:
                typer.echo(str(written))
