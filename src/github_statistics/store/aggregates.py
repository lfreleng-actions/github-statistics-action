# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Read-only aggregate queries the series builder counts from (design §7.3).

Each query groups in SQL, by day and actor, so the caller classifies
the distinct ``(login, type)`` pairs rather than every row; an event
counts only on a day its repository was in scope.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from . import codec
from .activity import ActivityStore
from .base import marks
from .models import ActorDayCount, RunDayCount

_ITEM_KINDS = ("issue", "pr")
_ITEM_DAY_COLUMNS = ("created_at", "closed_at")
SCHEDULE_EVENT = "schedule"


def _in_scope(table: str, column: str) -> str:
    """SQL true when the row's repository was in scope on the day of ``column``."""
    return (
        "EXISTS (SELECT 1 FROM scope WHERE scope.repo = "
        f"{table}.repo AND scope.from_day <= substr({table}.{column}, 1, 10)"
        f" AND (scope.to_day IS NULL OR scope.to_day > substr({table}.{column}, 1, 10)))"
    )


class AggregateStore(ActivityStore):
    """Grouped, read-only counts over items and runs."""

    def item_day_counts(
        self, kind: str, column: str, from_day: date, to_day: date
    ) -> list[ActorDayCount]:
        """Items of ``kind`` per day of ``column`` and author, in scope that day.

        ``kind`` is ``"issue"`` or ``"pr"``; ``column`` is ``"created_at"``
        or ``"closed_at"``. Days run from ``from_day`` to ``to_day``.
        """
        if kind not in _ITEM_KINDS:
            raise ValueError(f"unknown item kind {kind!r}")
        if column not in _ITEM_DAY_COLUMNS:
            raise ValueError(f"cannot count items by {column!r}")
        low, high = codec.day_bounds(from_day, to_day)
        rows = self._connection.execute(
            f"SELECT substr({column}, 1, 10) AS day, author, author_type, COUNT(*)"
            f" FROM items WHERE kind = ? AND {column} >= ? AND {column} < ?"
            f" AND {_in_scope('items', column)}"
            " GROUP BY day, author, author_type ORDER BY day, author, author_type",
            (kind, low, high),
        )
        return [
            ActorDayCount(codec.parse_day(day), login, account_type, count)
            for day, login, account_type, count in rows
        ]

    def run_day_counts(self, from_day: date, to_day: date) -> list[RunDayCount]:
        """Runs and summed jobs per creation day, actor and conclusion, in scope.

        A scheduled run is grouped with no actor, since GitHub names
        whoever last edited the schedule. A run without a job count adds
        nothing to ``jobs``.
        """
        low, high = codec.day_bounds(from_day, to_day)
        scheduled = "event = ?"
        rows = self._connection.execute(
            "SELECT substr(created_at, 1, 10) AS day,"
            f" CASE WHEN {scheduled} THEN NULL ELSE actor END AS login,"
            f" CASE WHEN {scheduled} THEN NULL ELSE actor_type END AS login_type,"
            f" {scheduled} AS is_scheduled, conclusion,"
            " COUNT(*), COALESCE(SUM(jobs), 0)"
            " FROM runs WHERE created_at >= ? AND created_at < ?"
            f" AND {_in_scope('runs', 'created_at')}"
            " GROUP BY day, login, login_type, is_scheduled, conclusion"
            " ORDER BY day, login, login_type, is_scheduled, conclusion",
            (SCHEDULE_EVENT, SCHEDULE_EVENT, SCHEDULE_EVENT, low, high),
        )
        return [
            RunDayCount(
                codec.parse_day(day),
                login,
                account_type,
                bool(is_scheduled),
                conclusion,
                runs,
                jobs,
            )
            for day, login, account_type, is_scheduled, conclusion, runs, jobs in rows
        ]

    def days_missing_jobs(self, from_day: date, to_day: date) -> list[tuple[str, date]]:
        """``(repo, day)`` pairs with a concluded run whose job count is unread."""
        low, high = codec.day_bounds(from_day, to_day)
        rows = self._connection.execute(
            "SELECT DISTINCT repo, substr(created_at, 1, 10) AS day FROM runs"
            " WHERE jobs IS NULL AND conclusion IS NOT NULL"
            " AND created_at >= ? AND created_at < ? ORDER BY repo, day",
            (low, high),
        )
        return [(repo, codec.parse_day(day)) for repo, day in rows]

    def actors(self) -> list[tuple[str, str | None]]:
        """Every distinct ``(login, type)`` the store credits anything to, sorted.

        Authors, mergers, closers, reviewers and the actors of runs that
        were not scheduled; logins GitHub reported as missing are left out.
        """
        rows = self._connection.execute(
            "SELECT author, author_type FROM items"
            " UNION SELECT merged_by, merged_by_type FROM items"
            " UNION SELECT closed_by, closed_by_type FROM items"
            " UNION SELECT reviewer, reviewer_type FROM reviews"
            " UNION SELECT actor, actor_type FROM runs WHERE event != ?",
            (SCHEDULE_EVENT,),
        )
        pairs = {(login, kind) for login, kind in rows if login is not None}
        return sorted(pairs, key=lambda pair: (pair[0], pair[1] or ""))

    def member_logins(self, orgs: Iterable[str]) -> list[str]:
        """The distinct stored public members of any of ``orgs``, sorted.

        Organisation names match whatever their case, as GitHub's do, so
        a configured ``ACME`` finds the members stored under ``acme``.
        """
        folded = sorted({org.lower() for org in orgs})
        if not folded:
            return []
        rows = self._connection.execute(
            "SELECT DISTINCT login FROM members"
            f" WHERE lower(org) IN ({marks(len(folded))}) ORDER BY login",
            folded,
        )
        return [login for (login,) in rows]
