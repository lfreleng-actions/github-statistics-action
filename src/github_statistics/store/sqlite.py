# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The SQLite store: one connection, every query (design §6.1)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta

from . import codec
from .aggregates import AggregateStore
from .base import assignments, marks
from .models import DayRange, Item, Member, Run, SyncState
from .schema import REPOSITORY_TABLES, TABLES

_UPSERT_SYNC = (
    f"INSERT INTO sync_state ({', '.join(codec.SYNC_COLUMNS)}) "
    f"VALUES ({marks(len(codec.SYNC_COLUMNS))}) "
    "ON CONFLICT (repo, metric) DO UPDATE SET " + assignments(codec.SYNC_COLUMNS[2:])
)


class Store(AggregateStore):
    """One SQLite store, used as a context manager.

    Every write method runs in one transaction, so an exception, or a
    crash, part-way through a write leaves the previous state.
    ``Store(Path(":memory:"))`` gives an in-memory store.
    """

    # Members

    def replace_members(self, org: str, logins: Iterable[str], today: date) -> None:
        """Make ``logins`` the public members of ``org`` as seen ``today``.

        Organisation names match whatever their case, as GitHub's do, so
        the rows of every spelling of ``org`` are replaced and stored
        under ``org``. Logins already stored keep their earliest
        ``first_seen``; logins no longer listed are deleted.
        """
        current = sorted(set(logins))
        seen = codec.day(today)
        with self._transaction() as connection:
            first_seen: dict[str, str] = {}
            for login, first in connection.execute(
                "SELECT login, first_seen FROM members WHERE lower(org) = lower(?)",
                (org,),
            ):
                first_seen[login] = min(first_seen.get(login, first), first)
            connection.execute(
                "DELETE FROM members WHERE lower(org) = lower(?)", (org,)
            )
            connection.executemany(
                "INSERT INTO members (org, login, first_seen, last_seen)"
                " VALUES (?, ?, ?, ?)",
                ((org, login, first_seen.get(login, seen), seen) for login in current),
            )

    def members(self, org: str) -> list[Member]:
        """The stored public members of ``org``, by login."""
        rows = self._connection.execute(
            "SELECT * FROM members WHERE org = ? ORDER BY login", (org,)
        )
        return [codec.member_from_row(row) for row in rows]

    # Sync state

    def sync_state(self, repo: str, metric: str) -> SyncState | None:
        """The sync state of one metric of one repository, if any."""
        row = self._connection.execute(
            "SELECT * FROM sync_state WHERE repo = ? AND metric = ?", (repo, metric)
        ).fetchone()
        return None if row is None else codec.sync_from_row(row)

    def set_sync_state(self, state: SyncState) -> None:
        """Store ``state``, replacing any earlier state for its repository and metric."""
        with self._transaction() as connection:
            connection.execute(_UPSERT_SYNC, codec.sync_row(state))

    def unfinished(self, metric: str, from_day: date, to_day: date) -> list[str]:
        """The repositories whose ``metric`` sync is unfinished, sorted.

        Only those in scope on some day from ``from_day`` to ``to_day``;
        an interval that opened and closed on one day holds no day.
        """
        rows = self._connection.execute(
            "SELECT DISTINCT s.repo FROM sync_state AS s JOIN scope AS c"
            " ON c.repo = s.repo WHERE s.metric = ? AND s.unfinished"
            " AND c.from_day <= ? AND (c.to_day IS NULL"
            " OR (c.to_day > ? AND c.to_day > c.from_day))"
            " ORDER BY s.repo",
            (metric, codec.day(to_day), codec.day(from_day)),
        )
        return [row[0] for row in rows]

    def store_item_page(self, items: Iterable[Item], state: SyncState) -> None:
        """Upsert one listing page of ``items`` and ``state`` in one transaction.

        The watermark in ``state`` then never runs ahead of the items it
        describes, nor falls behind them (design §6.5). An item listed
        with ``updated_at`` equal to its ``reviews_read_at`` has that
        marker cleared: both hold whole seconds, so an update in the
        second of the read cannot be told from one before it, and
        enrichment reads the item again (design §6.3).
        """
        page = list(items)
        with self._transaction() as connection:
            self.upsert_items(page)
            connection.executemany(
                "UPDATE items SET reviews_read_at = NULL"
                " WHERE repo = ? AND number = ? AND reviews_read_at = updated_at",
                ((item.repo, item.number) for item in page),
            )
            self.set_sync_state(state)

    def store_run_page(self, runs: Iterable[Run], state: SyncState) -> None:
        """Upsert ``runs`` and store ``state`` in one transaction.

        The watermark in ``state`` then never runs ahead of the runs it
        describes, nor falls behind them (design §6.5).
        """
        with self._transaction():
            self.upsert_runs(runs)
            self.set_sync_state(state)

    def restart_item_sync(
        self, state: SyncState, reread_before: datetime | None = None
    ) -> None:
        """Store a restarted ``state`` and, given ``reread_before``, mark reviews.

        With ``reread_before``, the pull requests of ``state.repo`` created
        before it lose their ``reviews_read_at``: a prune drops reviews
        submitted before its day but keeps a pull request updated since,
        so a wider window reads the dropped reviews again. Both commit in
        one transaction, before any page is requested, so a retry that
        lists nothing does not mark pull requests already read again.
        """
        with self._transaction() as connection:
            if reread_before is not None:
                connection.execute(
                    "UPDATE items SET reviews_read_at = NULL"
                    " WHERE repo = ? AND kind = 'pr' AND created_at < ?",
                    (state.repo, codec.timestamp(reread_before)),
                )
            self.set_sync_state(state)

    # Coverage

    def add_coverage(
        self, repo: str, metric: str, from_day: date, to_day: date
    ) -> None:
        """Mark ``from_day`` to ``to_day`` complete, merging touching ranges."""
        if to_day < from_day:
            raise ValueError(
                f"coverage range ends before it starts: {from_day}..{to_day}"
            )
        one = timedelta(days=1)
        with self._transaction() as connection:
            rows = list(
                connection.execute(
                    "SELECT from_day, to_day FROM coverage"
                    " WHERE repo = ? AND metric = ? AND from_day <= ? AND to_day >= ?",
                    (repo, metric, codec.day(to_day + one), codec.day(from_day - one)),
                )
            )
            start = min([codec.day(from_day), *(row[0] for row in rows)])
            end = max([codec.day(to_day), *(row[1] for row in rows)])
            connection.executemany(
                "DELETE FROM coverage WHERE repo = ? AND metric = ? AND from_day = ?",
                ((repo, metric, row[0]) for row in rows),
            )
            connection.execute(
                "INSERT INTO coverage (repo, metric, from_day, to_day)"
                " VALUES (?, ?, ?, ?)",
                (repo, metric, start, end),
            )

    def coverage(self, repo: str, metric: str) -> list[DayRange]:
        """The complete day ranges of one metric of one repository, in order."""
        rows = self._connection.execute(
            "SELECT from_day, to_day FROM coverage WHERE repo = ? AND metric = ?"
            " ORDER BY from_day",
            (repo, metric),
        )
        return [DayRange(codec.parse_day(a), codec.parse_day(b)) for a, b in rows]

    def covered(self, repo: str, metric: str, day: date) -> bool:
        """Whether ``day`` lies in a complete range of the metric."""
        text = codec.day(day)
        row = self._connection.execute(
            "SELECT 1 FROM coverage WHERE repo = ? AND metric = ?"
            " AND from_day <= ? AND to_day >= ? LIMIT 1",
            (repo, metric, text, text),
        ).fetchone()
        return row is not None

    # Scope

    def enter_scope(self, repo: str, from_day: date) -> None:
        """Record ``repo`` in scope from ``from_day``; a no-op while already in scope.

        Re-entering on or before the day a closed interval ended reopens
        that interval rather than starting a new one.
        """
        text = codec.day(from_day)
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM scope WHERE repo = ? AND to_day IS NULL", (repo,)
            ).fetchone():
                return
            reopened = connection.execute(
                "UPDATE scope SET to_day = NULL WHERE repo = ?"
                " AND from_day <= ? AND to_day >= ?",
                (repo, text, text),
            )
            if reopened.rowcount == 0:
                connection.execute(
                    "INSERT INTO scope (repo, from_day, to_day) VALUES (?, ?, NULL)",
                    (repo, text),
                )

    def leave_scope(self, repo: str, day: date) -> None:
        """Record ``repo`` out of scope from ``day`` on; ``day`` itself is out.

        An interval that opened on ``day`` closes empty rather than going,
        so the store still records that ``repo`` was seen.
        """
        text = codec.day(day)
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM scope WHERE repo = ? AND to_day IS NULL AND from_day > ?",
                (repo, text),
            )
            connection.execute(
                "UPDATE scope SET to_day = ? WHERE repo = ? AND to_day IS NULL",
                (text, repo),
            )

    def in_scope(self, day: date) -> list[str]:
        """The repositories in scope on ``day``, sorted."""
        text = codec.day(day)
        rows = self._connection.execute(
            "SELECT DISTINCT repo FROM scope WHERE from_day <= ?"
            " AND (to_day IS NULL OR to_day > ?) ORDER BY repo",
            (text, text),
        )
        return [row[0] for row in rows]

    def scope_intervals(self, repo: str) -> list[tuple[date, date | None]]:
        """The scope intervals of ``repo`` by start; the end is ``None`` while open."""
        rows = self._connection.execute(
            "SELECT from_day, to_day FROM scope WHERE repo = ? ORDER BY from_day",
            (repo,),
        )
        return [
            (codec.parse_day(a), None if b is None else codec.parse_day(b))
            for a, b in rows
        ]

    def stored_repositories(self) -> list[str]:
        """Every repository with a row in any per-repository table, sorted."""
        union = " UNION ".join(f"SELECT repo FROM {t}" for t in REPOSITORY_TABLES)
        rows = self._connection.execute(f"{union} ORDER BY repo")
        return [row[0] for row in rows]

    # Privacy and housekeeping

    def purge_repository(self, repo: str) -> None:
        """Delete every row for ``repo`` from every table (design §5)."""
        with self._transaction() as connection:
            for table in REPOSITORY_TABLES:
                connection.execute(f"DELETE FROM {table} WHERE repo = ?", (repo,))

    def prune(self, before_day: date) -> dict[str, int]:
        """Drop history that ended before ``before_day``; return rows deleted per table.

        Items go once created, closed and last updated before the day, so
        an item still open stays, and so does one updated since, which
        enrichment may still re-read; reviews and runs go by their own timestamps;
        coverage ranges are clipped to start on the day; scope intervals
        that ended by the day go, except each repository's latest, which
        stays so a repository that returns is known to have been seen.
        Each sync has its ``oldest`` raised to the day, since the store no
        longer holds what it read before it.
        """
        low, _ = codec.day_bounds(before_day, before_day)
        text = codec.day(before_day)
        deleted: dict[str, int] = {}
        with self._transaction() as connection:
            deleted["items"] = connection.execute(
                "DELETE FROM items WHERE created_at < ?"
                " AND closed_at IS NOT NULL AND closed_at < ? AND updated_at < ?",
                (low, low, low),
            ).rowcount
            deleted["reviews"] = connection.execute(
                "DELETE FROM reviews WHERE submitted_at < ?", (low,)
            ).rowcount
            deleted["runs"] = connection.execute(
                "DELETE FROM runs WHERE created_at < ?", (low,)
            ).rowcount
            deleted["coverage"] = connection.execute(
                "DELETE FROM coverage WHERE to_day < ?", (text,)
            ).rowcount
            connection.execute(
                "UPDATE coverage SET from_day = ? WHERE from_day < ?", (text, text)
            )
            connection.execute(
                "UPDATE sync_state SET oldest = ? WHERE oldest < ?",
                (low, low),
            )
            deleted["scope"] = connection.execute(
                "DELETE FROM scope WHERE to_day IS NOT NULL AND to_day <= ?"
                " AND from_day < (SELECT MAX(from_day) FROM scope AS latest"
                " WHERE latest.repo = scope.repo)",
                (text,),
            ).rowcount
        return deleted

    def counts(self) -> dict[str, int]:
        """Row count of each table."""
        return {
            table: self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[
                0
            ]
            for table in TABLES
        }
