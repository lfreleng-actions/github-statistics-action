# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Helpers the commands share: the read-only store, failures and GitHub files.

A command reports a failure with `fail` and raises the exit it returns.
Stores open through `ReadOnlyStore` for every command but ``collect``,
so reading never creates, rebuilds or writes one. `write_outputs` and
`append_summary` reach the files GitHub Actions names in
``$GITHUB_OUTPUT`` and ``$GITHUB_STEP_SUMMARY`` and do nothing when
those are unset.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import typer

from github_statistics.client.transport import DEFAULT_API_URL
from github_statistics.store import SCHEMA_VERSION, Store

API_HOST = urlsplit(DEFAULT_API_URL).hostname or "api.github.com"

_OUTPUT_KEY = re.compile(r"[A-Za-z0-9_]+")


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


def fail(message: str, code: int = 1) -> typer.Exit:
    """Report ``message`` on standard error; the exit with ``code`` to raise."""
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code)


def write_outputs(outputs: Mapping[str, str]) -> None:
    """Append ``outputs`` to ``$GITHUB_OUTPUT`` as ``key=value`` lines, when set.

    A key outside ``[A-Za-z0-9_]``, or a value holding a line break or
    ``<<``, could start another output or a multiline one, so the whole
    set is refused before anything is written.
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    for key, value in outputs.items():
        if not _OUTPUT_KEY.fullmatch(key):
            raise fail(f"refusing the output name {key!r}")
        if "\n" in value or "\r" in value or "<<" in value:
            raise fail(f"refusing the value of output {key}: it could inject another")
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def append_summary(markdown: str) -> None:
    """Append ``markdown`` to ``$GITHUB_STEP_SUMMARY``, when set."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown)
