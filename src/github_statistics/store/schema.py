# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The store's tables, their version stamp, and read-only inspection.

A store stamped with an older version the code knows is migrated in
place, keeping its rows; one with a version the code does not know,
newer or with no migration path, is dropped and recreated empty.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 2

_SCHEMA = (
    """
    CREATE TABLE items (
        repo TEXT NOT NULL,
        number INTEGER NOT NULL,
        node_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('issue', 'pr')),
        created_at TEXT NOT NULL,
        closed_at TEXT,
        merged_at TEXT,
        author TEXT,
        author_type TEXT,
        merged_by TEXT,
        merged_by_type TEXT,
        closed_by TEXT,
        closed_by_type TEXT,
        updated_at TEXT NOT NULL,
        reviews_read_at TEXT,
        PRIMARY KEY (repo, number)
    )
    """,
    """
    CREATE TABLE reviews (
        id INTEGER PRIMARY KEY,
        node_id TEXT NOT NULL,
        repo TEXT NOT NULL,
        number INTEGER NOT NULL,
        reviewer TEXT,
        reviewer_type TEXT,
        state TEXT NOT NULL,
        submitted_at TEXT
    )
    """,
    "CREATE INDEX reviews_by_item ON reviews (repo, number)",
    """
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY,
        node_id TEXT NOT NULL,
        repo TEXT NOT NULL,
        workflow_path TEXT NOT NULL,
        event TEXT NOT NULL,
        status TEXT NOT NULL,
        conclusion TEXT,
        created_at TEXT NOT NULL,
        actor TEXT,
        actor_type TEXT,
        attempt INTEGER NOT NULL,
        jobs INTEGER
    )
    """,
    "CREATE INDEX runs_by_repo ON runs (repo, created_at)",
    "CREATE INDEX runs_needing_jobs ON runs (created_at, id) WHERE jobs IS NULL",
    """
    CREATE TABLE members (
        org TEXT NOT NULL,
        login TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        PRIMARY KEY (org, login)
    )
    """,
    """
    CREATE TABLE sync_state (
        repo TEXT NOT NULL,
        metric TEXT NOT NULL,
        watermark TEXT,
        etag TEXT,
        oldest TEXT,
        last_sync_at TEXT,
        swept_to TEXT,
        unfinished INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (repo, metric)
    )
    """,
    """
    CREATE TABLE coverage (
        repo TEXT NOT NULL,
        metric TEXT NOT NULL,
        from_day TEXT NOT NULL,
        to_day TEXT NOT NULL,
        PRIMARY KEY (repo, metric, from_day)
    )
    """,
    """
    CREATE TABLE scope (
        repo TEXT NOT NULL,
        from_day TEXT NOT NULL,
        to_day TEXT,
        PRIMARY KEY (repo, from_day)
    )
    """,
)

# One second before a repository's earliest stored update, as the
# collector overlaps ``since``, which may exclude an update in that second.
_EARLIEST = (
    "(SELECT strftime('%Y-%m-%dT%H:%M:%SZ', MIN(updated_at), '-1 second')"
    " FROM items WHERE items.repo = sync_state.repo)"
)

# The statements that bring a store at each version to the next. A
# version-1 store cannot tell whether an item sync stopped short, nor
# which earlier scan its rows came from, so every repository with items
# gets an item sync state, which a version-1 `collect --refresh` stopped
# before its first page leaves out, and each one starts unfinished with
# no watermark and an `oldest` no later than its earliest stored update.
# The next scan rereads every stored item before a drained listing
# clears the mark.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        "ALTER TABLE sync_state ADD COLUMN unfinished INTEGER NOT NULL DEFAULT 0",
        "INSERT INTO sync_state (repo, metric) SELECT DISTINCT repo, 'items'"
        " FROM items WHERE repo NOT IN"
        " (SELECT repo FROM sync_state WHERE metric = 'items')",
        "UPDATE sync_state SET unfinished = 1, watermark = NULL,"
        f" oldest = COALESCE(MIN(oldest, {_EARLIEST}), oldest, {_EARLIEST})"
        " WHERE metric = 'items'",
    ),
}

TABLES = ("items", "reviews", "runs", "members", "sync_state", "coverage", "scope")
REPOSITORY_TABLES = ("items", "reviews", "runs", "sync_state", "coverage", "scope")


def create_schema(connection: sqlite3.Connection) -> None:
    """Drop every table and create the current schema, stamped with its version.

    Call inside a transaction. Statements run one at a time;
    ``executescript`` would commit first and could leave a half-built
    schema behind.
    """
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name NOT GLOB 'sqlite_*'"
        )
    ]
    for name in names:
        quoted = name.replace('"', '""')
        connection.execute(f'DROP TABLE "{quoted}"')
    for statement in _SCHEMA:
        connection.execute(statement)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def migrate_schema(connection: sqlite3.Connection, version: int) -> None:
    """Bring a store at ``version`` to `SCHEMA_VERSION`, keeping its rows.

    Applies each version's `MIGRATIONS` in order, then stamps the
    version. A version with no path to `SCHEMA_VERSION`, newer than it
    or never stamped, gets `create_schema` instead. Call inside a
    transaction, as for `create_schema`.
    """
    path = range(version, SCHEMA_VERSION)
    if not path or any(step not in MIGRATIONS for step in path):
        create_schema(connection)
        return
    for step in path:
        for statement in MIGRATIONS[step]:
            connection.execute(statement)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@dataclass(frozen=True)
class StoreInfo:
    """What `describe` reports about one store file."""

    path: Path
    size: int
    schema_version: int
    counts: dict[str, int]


def describe(path: Path) -> StoreInfo:
    """Size, schema version and row counts of the store at ``path``, read-only.

    Unlike opening a `Store`, this never changes the file, so it reports
    a store stamped with another schema version as it stands.
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        present = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES
            if table in present
        }
    finally:
        connection.close()
    return StoreInfo(Path(path), Path(path).stat().st_size, version, counts)
