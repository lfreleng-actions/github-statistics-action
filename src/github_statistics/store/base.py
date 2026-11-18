# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The connection, transaction and chunked-query plumbing every store shares."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from .schema import SCHEMA_VERSION, create_schema

# Older SQLite builds bind at most 999 parameters in one statement; 500
# leaves room for the other parameters of a query.
CHUNK = 500


def chunks(values: Sequence[Any]) -> Iterator[Sequence[Any]]:
    """``values`` in slices of at most `CHUNK`."""
    for start in range(0, len(values), CHUNK):
        yield values[start : start + CHUNK]


def marks(count: int) -> str:
    """``?, ?, ...`` with ``count`` placeholders."""
    return ", ".join("?" * count)


def assignments(columns: Iterable[str]) -> str:
    """``column = excluded.column`` for each of ``columns``."""
    return ", ".join(f"{column} = excluded.{column}" for column in columns)


class StoreBase:
    """The connection and transaction handling under `Store`."""

    def __init__(self, path: Path) -> None:
        """Open, creating it and its directory if needed, the store at ``path``."""
        self.path = Path(path)
        self._savepoints = 0
        if str(path) == ":memory:":
            target = ":memory:"
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target = str(self.path)
        self._connection = sqlite3.connect(target, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        try:
            self._ensure_schema()
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> Self:
        """Return the store itself."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection."""
        self.close()

    def close(self) -> None:
        """Close the connection."""
        self._connection.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection, for inspection."""
        return self._connection

    @contextmanager
    def _transaction(self, mode: str = "") -> Iterator[sqlite3.Connection]:
        """Run the block in one transaction; roll back on any exception.

        ``mode`` is ``""`` or ``"IMMEDIATE"``, which takes the write lock
        at once. A failed ``COMMIT`` rolls back too, so no transaction is
        left open for a later write to join and never commit. Inside an
        open transaction the block runs under a savepoint instead, so a
        failure undoes that block alone and the caller may carry on.
        """
        connection = self._connection
        if connection.in_transaction:
            with self._savepoint(connection):
                yield connection
            return
        connection.execute(f"BEGIN {mode}".strip())
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    @contextmanager
    def _savepoint(self, connection: sqlite3.Connection) -> Iterator[None]:
        """Run the block under a savepoint; roll back to it on any exception."""
        self._savepoints += 1
        name = f"write_{self._savepoints}"
        connection.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            # SQLite may already have rolled back the whole transaction.
            if connection.in_transaction:
                connection.execute(f"ROLLBACK TO {name}")
                connection.execute(f"RELEASE {name}")
            raise
        else:
            connection.execute(f"RELEASE {name}")
        finally:
            self._savepoints -= 1

    def schema_version(self) -> int:
        """The schema version stamped in the file."""
        return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    def _ensure_schema(self) -> None:
        """Drop and recreate every table unless the file carries `SCHEMA_VERSION`."""
        if self.schema_version() == SCHEMA_VERSION:
            return
        # Another process may rebuild the store between the check above and
        # taking the write lock; check again under the lock so its rows stay.
        with self._transaction("IMMEDIATE") as connection:
            if self.schema_version() != SCHEMA_VERSION:
                create_schema(connection)

    def _select_in(
        self,
        sql: str,
        params: Sequence[Any],
        column: str,
        values: Sequence[Any] | None,
        order: str,
    ) -> list[sqlite3.Row]:
        """Rows of ``sql``, restricted to ``column IN values`` when given.

        The IN list is chunked at `CHUNK` parameters; ``order`` sorts the
        combined result.
        """
        if values is None:
            return list(self._connection.execute(f"{sql} ORDER BY {order}", params))
        rows: list[sqlite3.Row] = []
        unique = sorted(set(values))
        for chunk in chunks(unique):
            query = f"{sql} AND {column} IN ({marks(len(chunk))})"
            rows.extend(self._connection.execute(query, [*params, *chunk]))
        keys = [part.strip() for part in order.split(",")]
        rows.sort(key=lambda row: tuple(_sortable(row[key]) for key in keys))
        return rows


def _sortable(value: Any) -> tuple[bool, Any]:
    """A sort key that places ``None`` first and compares the rest."""
    return (value is not None, value)
