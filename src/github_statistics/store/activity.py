# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Store methods for issues, pull requests, reviews and workflow runs."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta

from . import codec
from .base import StoreBase, assignments, chunks, marks
from .models import Enrichment, Item, Review, Run

_ITEM_LISTING = (
    "node_id",
    "kind",
    "created_at",
    "closed_at",
    "merged_at",
    "author",
    "author_type",
    "updated_at",
)
_ITEM_ENRICHED = (
    "merged_by",
    "merged_by_type",
    "closed_by",
    "closed_by_type",
    "reviews_read_at",
)
_UPSERT_ITEM = (
    f"INSERT INTO items ({', '.join(codec.ITEM_COLUMNS)}) "
    f"VALUES ({marks(len(codec.ITEM_COLUMNS))}) "
    "ON CONFLICT (repo, number) DO UPDATE SET "
    + assignments(_ITEM_LISTING)
    + ", "
    + ", ".join(
        f"{column} = COALESCE(excluded.{column}, items.{column})"
        for column in _ITEM_ENRICHED
    )
)
_UPSERT_RUN = (
    f"INSERT INTO runs ({', '.join(codec.RUN_COLUMNS)}) "
    f"VALUES ({marks(len(codec.RUN_COLUMNS))}) "
    "ON CONFLICT (id) DO UPDATE SET "
    + assignments(c for c in codec.RUN_COLUMNS if c not in ("id", "jobs"))
    + ", jobs = CASE WHEN runs.status IS NOT excluded.status"
    " OR runs.conclusion IS NOT excluded.conclusion"
    " OR runs.attempt IS NOT excluded.attempt"
    " THEN NULL ELSE COALESCE(excluded.jobs, runs.jobs) END"
)
# The coverage metric a run's job count completes (design §6.4).
JOBS_METRIC = "jobs"

_INSERT_REVIEW = (
    f"INSERT OR REPLACE INTO reviews ({', '.join(codec.REVIEW_COLUMNS)}) "
    f"VALUES ({marks(len(codec.REVIEW_COLUMNS))})"
)


class ActivityStore(StoreBase):
    """Items, reviews and runs: the facts the charts count."""

    # Items

    def upsert_items(self, items: Iterable[Item]) -> None:
        """Insert or update items from a listing.

        Listing fields are overwritten; enrichment fields keep their
        stored values unless the record carries new ones.
        """
        with self._transaction() as connection:
            connection.executemany(_UPSERT_ITEM, (codec.item_row(i) for i in items))

    def items_needing_enrichment(self, repo: str, limit: int) -> list[Item]:
        """Up to ``limit`` items of ``repo`` never read, or updated since last read."""
        rows = self._connection.execute(
            "SELECT * FROM items WHERE repo = ?"
            " AND (reviews_read_at IS NULL OR updated_at > reviews_read_at)"
            " ORDER BY number LIMIT ?",
            (repo, limit),
        )
        return [codec.item_from_row(row) for row in rows]

    def mark_enriched(self, enrichments: Iterable[Enrichment]) -> None:
        """Record mergers, closers and the read time for each item."""
        with self._transaction() as connection:
            connection.executemany(
                "UPDATE items SET merged_by = ?, merged_by_type = ?,"
                " closed_by = ?, closed_by_type = ?, reviews_read_at = ?"
                " WHERE repo = ? AND number = ?",
                (
                    (
                        e.merged_by,
                        e.merged_by_type,
                        e.closed_by,
                        e.closed_by_type,
                        codec.timestamp(e.read_at),
                        e.repo,
                        e.number,
                    )
                    for e in enrichments
                ),
            )

    def items_between(
        self, from_day: date, to_day: date, repos: Sequence[str] | None = None
    ) -> list[Item]:
        """Items created, closed or merged on a day from ``from_day`` to ``to_day``."""
        low, high = codec.day_bounds(from_day, to_day)
        sql = (
            "SELECT * FROM items WHERE ("
            "(created_at >= ? AND created_at < ?)"
            " OR (closed_at >= ? AND closed_at < ?)"
            " OR (merged_at >= ? AND merged_at < ?))"
        )
        rows = self._select_in(sql, (low, high) * 3, "repo", repos, "repo, number")
        return [codec.item_from_row(row) for row in rows]

    # Reviews

    def replace_reviews(
        self, repo: str, number: int, reviews: Iterable[Review]
    ) -> None:
        """Replace the stored reviews of one pull request with ``reviews``."""
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM reviews WHERE repo = ? AND number = ?", (repo, number)
            )
            connection.executemany(
                _INSERT_REVIEW, (codec.review_row(r) for r in reviews)
            )

    def reviews_for(self, repo: str, number: int) -> list[Review]:
        """The stored reviews of one pull request."""
        rows = self._connection.execute(
            "SELECT * FROM reviews WHERE repo = ? AND number = ? ORDER BY id",
            (repo, number),
        )
        return [codec.review_from_row(row) for row in rows]

    def reviews_between(
        self, from_day: date, to_day: date, repos: Sequence[str] | None = None
    ) -> list[Review]:
        """Reviews submitted on a day from ``from_day`` to ``to_day``."""
        low, high = codec.day_bounds(from_day, to_day)
        sql = "SELECT * FROM reviews WHERE submitted_at >= ? AND submitted_at < ?"
        rows = self._select_in(sql, (low, high), "repo", repos, "submitted_at, id")
        return [codec.review_from_row(row) for row in rows]

    # Runs

    def upsert_runs(self, runs: Iterable[Run]) -> None:
        """Insert or update runs; a changed status, conclusion or attempt clears ``jobs``.

        Each run left without a job count takes its creation day out of
        the jobs coverage of its repository, so the day stays incomplete
        until the count is read again and coverage is added back.
        """
        rows = [codec.run_row(r) for r in runs]
        ids = sorted({run_id for run_id, *_ in rows})
        with self._transaction() as connection:
            connection.executemany(_UPSERT_RUN, rows)
            days: set[tuple[str, str]] = set()
            for chunk in chunks(ids):
                days.update(
                    (repo, day)
                    for repo, day in connection.execute(
                        "SELECT repo, substr(created_at, 1, 10) FROM runs"
                        f" WHERE jobs IS NULL AND id IN ({marks(len(chunk))})",
                        chunk,
                    )
                )
            for repo, day in sorted(days):
                _uncover(connection, repo, JOBS_METRIC, day)

    def runs_needing_jobs(self, limit: int) -> list[Run]:
        """Up to ``limit`` runs, oldest first, whose job count is not yet read."""
        rows = self._connection.execute(
            "SELECT * FROM runs WHERE jobs IS NULL ORDER BY created_at, id LIMIT ?",
            (limit,),
        )
        return [codec.run_from_row(row) for row in rows]

    def pending_runs(self, repo: str) -> list[Run]:
        """Runs of ``repo`` that have not completed, to re-read on the next sync."""
        rows = self._connection.execute(
            "SELECT * FROM runs WHERE repo = ? AND status != 'completed'"
            " ORDER BY created_at, id",
            (repo,),
        )
        return [codec.run_from_row(row) for row in rows]

    def set_job_counts(self, counts: Mapping[int, int]) -> None:
        """Record the job count of each run id in ``counts``."""
        with self._transaction() as connection:
            connection.executemany(
                "UPDATE runs SET jobs = ? WHERE id = ?",
                ((jobs, run_id) for run_id, jobs in counts.items()),
            )

    def runs_between(
        self, from_day: date, to_day: date, repos: Sequence[str] | None = None
    ) -> list[Run]:
        """Runs created on a day from ``from_day`` to ``to_day``."""
        low, high = codec.day_bounds(from_day, to_day)
        sql = "SELECT * FROM runs WHERE created_at >= ? AND created_at < ?"
        rows = self._select_in(sql, (low, high), "repo", repos, "created_at, id")
        return [codec.run_from_row(row) for row in rows]


def _uncover(connection: sqlite3.Connection, repo: str, metric: str, day: str) -> None:
    """Take ``day`` out of the coverage of ``metric``, splitting its range."""
    row = connection.execute(
        "SELECT from_day, to_day FROM coverage"
        " WHERE repo = ? AND metric = ? AND from_day <= ? AND to_day >= ?",
        (repo, metric, day, day),
    ).fetchone()
    if row is None:
        return
    start, end = row
    one = timedelta(days=1)
    pieces = []
    if start < day:
        pieces.append((start, codec.day(codec.parse_day(day) - one)))
    if day < end:
        pieces.append((codec.day(codec.parse_day(day) + one), end))
    connection.execute(
        "DELETE FROM coverage WHERE repo = ? AND metric = ? AND from_day = ?",
        (repo, metric, start),
    )
    connection.executemany(
        "INSERT INTO coverage (repo, metric, from_day, to_day) VALUES (?, ?, ?, ?)",
        ((repo, metric, low, high) for low, high in pieces),
    )
