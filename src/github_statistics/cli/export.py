# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The ``export`` command: the dataset as JSON or CSV (design §4.2, §8.6).

It reads each target's store and nothing else: it needs no token,
makes no request, and never creates or rebuilds a store. Files go to
``<output>/<owner>/data/``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

import typer

from github_statistics import __version__
from github_statistics.client.transport import DEFAULT_API_URL
from github_statistics.config import ConfigError, default_cache_dir
from github_statistics.store import SCHEMA_VERSION, Store, path_component, store_path

from .options import SharedOptions, shared_options

DATA_DIR = "data"
API_HOST = urlsplit(DEFAULT_API_URL).hostname or "api.github.com"


class Format(StrEnum):
    """Which files ``export`` writes."""

    JSON = "json"
    CSV = "csv"


class StaleStoreError(Exception):
    """The store file carries a schema version other than `SCHEMA_VERSION`."""


class ReadOnlyStore(Store):
    """A `Store` on a read-only connection: it never creates, rebuilds or writes.

    One read transaction spans the store's life, from before the schema
    check until `close`, so every query reads the same snapshot and a
    collector committing meanwhile cannot make one document disagree
    with another. A file removed or replaced after the check cannot be
    recreated or rebuilt by this command. While the store is open a
    collector's commit waits on it, up to the collector's busy timeout.
    """

    def __init__(self, path: Path) -> None:
        """Open the store at ``path`` read-only; raise unless it is current.

        `sqlite3.DatabaseError` when no database can be read there,
        `StaleStoreError` when its schema version is not `SCHEMA_VERSION`.
        """
        self.path = Path(path)
        self._savepoints = 0
        uri = self.path.resolve().as_uri() + "?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("BEGIN")
            if self.schema_version() != SCHEMA_VERSION:
                raise StaleStoreError(str(self.path))
        except BaseException:
            self._connection.close()
            raise

    def close(self) -> None:
        """End the read transaction and close the connection."""
        if self._connection.in_transaction:
            self._connection.execute("ROLLBACK")
        self._connection.close()


def _fail(message: str) -> typer.Exit:
    """Report ``message`` on standard error; the exit to raise."""
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(1)


@shared_options
def export(
    options: SharedOptions,
    output_format: Format = typer.Option(
        Format.JSON, "--format", help="Write JSON documents or CSV tables."
    ),
) -> None:
    """Write each target's series, tables and coverage from the store; no network."""
    # Imported here so other commands start without loading matplotlib.
    from github_statistics.export import build_documents, write_csv, write_json

    try:
        resolution = options.resolve()
    except ConfigError as exc:
        raise _fail(str(exc)) from None
    config = resolution.config
    now = datetime.now(UTC).date()
    until = options.until or now - timedelta(days=1)
    cache = (
        Path(config.cache.directory) if config.cache.directory else default_cache_dir()
    )
    write = write_csv if output_format is Format.CSV else write_json
    for target in config.targets:
        path = store_path(cache, API_HOST, target.owner)
        if not path.is_file():
            raise _fail(f"no store for {target.owner} at {path}; run collect first")
        try:
            store = ReadOnlyStore(path)
        except sqlite3.DatabaseError as exc:
            raise _fail(f"cannot read the store at {path}: {exc}") from None
        except StaleStoreError:
            raise _fail(f"the store at {path} is stale; run collect first") from None
        with store:
            documents = build_documents(
                store,
                config,
                target,
                until=until,
                generated=now,
                version=__version__,
                identities=resolution.identities_for(target),
            )
        directory = (
            Path(config.output.directory)
            / path_component(target.owner, "owner")
            / DATA_DIR
        )
        for written in write(documents, directory):
            typer.echo(str(written))
