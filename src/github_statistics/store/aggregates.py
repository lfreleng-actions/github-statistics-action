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
_ITEM_EVENT_COLUMNS = ("created_at", "closed_at", "merged_at")
_ITEM_ACTORS = {
    "author": "author_type",
    "merged_by": "merged_by_type",
    "closed_by": "closed_by_type",
}
SCHEDULE_EVENT = "schedule"
DYNAMIC_EVENT = "dynamic"
# Run events that are automation rather than a person's work (design §7.3):
# such a run is grouped with no actor and credited to no login.
AUTOMATION_EVENTS = (SCHEDULE_EVENT, DYNAMIC_EVENT)
_AUTOMATION = f"event IN ({marks(len(AUTOMATION_EVENTS))})"

# One actor's count: ``(login, type, count)``.
ActorCount = tuple[str | None, str | None, int]


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

        A run of an automation event is grouped with no actor and its
        event as ``automation``: GitHub names whoever last edited a
        schedule, or whose action set off its own automation. A run
        without a job count adds nothing to ``jobs``.
        """
        low, high = codec.day_bounds(from_day, to_day)
        rows = self._connection.execute(
            "SELECT substr(created_at, 1, 10) AS day,"
            f" CASE WHEN {_AUTOMATION} THEN NULL ELSE actor END AS login,"
            f" CASE WHEN {_AUTOMATION} THEN NULL ELSE actor_type END AS login_type,"
            f" CASE WHEN {_AUTOMATION} THEN event END AS automation, conclusion,"
            " COUNT(*), COALESCE(SUM(jobs), 0)"
            " FROM runs WHERE created_at >= ? AND created_at < ?"
            f" AND {_in_scope('runs', 'created_at')}"
            " GROUP BY day, login, login_type, automation, conclusion"
            " ORDER BY day, login, login_type, automation, conclusion",
            (*(AUTOMATION_EVENTS * 3), low, high),
        )
        return [
            RunDayCount(
                codec.parse_day(day),
                login,
                account_type,
                automation,
                conclusion,
                runs,
                jobs,
            )
            for day, login, account_type, automation, conclusion, runs, jobs in rows
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

        Authors, mergers, closers, reviewers and the actors of runs of
        no automation event; logins GitHub reported as missing are left out.
        """
        rows = self._connection.execute(
            "SELECT author, author_type FROM items"
            " UNION SELECT merged_by, merged_by_type FROM items"
            " UNION SELECT closed_by, closed_by_type FROM items"
            " UNION SELECT reviewer, reviewer_type FROM reviews"
            f" UNION SELECT actor, actor_type FROM runs WHERE NOT {_AUTOMATION}",
            AUTOMATION_EVENTS,
        )
        pairs = {(login, kind) for login, kind in rows if login is not None}
        return sorted(pairs, key=lambda pair: (pair[0], pair[1] or ""))

    def item_actor_counts(
        self,
        kind: str | None,
        column: str,
        from_day: date,
        to_day: date,
        *,
        actor: str = "author",
        unmerged: bool = False,
    ) -> list[ActorCount]:
        """Items per ``actor`` whose ``column`` falls in the days, in scope that day.

        ``kind`` is ``"issue"``, ``"pr"`` or ``None`` for both; ``column``
        is ``created_at``, ``closed_at`` or ``merged_at``; ``actor`` is
        ``author``, ``merged_by`` or ``closed_by``. With ``unmerged`` only
        items never merged count. Rows are sorted by login and type.
        """
        if kind is not None and kind not in _ITEM_KINDS:
            raise ValueError(f"unknown item kind {kind!r}")
        if column not in _ITEM_EVENT_COLUMNS:
            raise ValueError(f"cannot count items by {column!r}")
        if actor not in _ITEM_ACTORS:
            raise ValueError(f"unknown item actor {actor!r}")
        actor_type = _ITEM_ACTORS[actor]
        low, high = codec.day_bounds(from_day, to_day)
        where = [f"{column} >= ?", f"{column} < ?", _in_scope("items", column)]
        params: list[str] = [low, high]
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        if unmerged:
            where.append("merged_at IS NULL")
        rows = self._connection.execute(
            f"SELECT {actor}, {actor_type}, COUNT(*) FROM items"
            f" WHERE {' AND '.join(where)}"
            f" GROUP BY {actor}, {actor_type} ORDER BY {actor}, {actor_type}",
            params,
        )
        return [(login, account_type, count) for login, account_type, count in rows]

    def review_actor_counts(
        self, from_day: date, to_day: date
    ) -> list[tuple[str | None, str | None, int, int, int]]:
        """Per reviewer: reviews, approvals and changes requested.

        Reviews submitted from ``from_day`` to ``to_day`` on a day their
        repository was in scope; rows are ``(login, type, reviews,
        approved, changes_requested)``, sorted by login.
        """
        low, high = codec.day_bounds(from_day, to_day)
        rows = self._connection.execute(
            "SELECT reviewer, reviewer_type, COUNT(*),"
            " SUM(state = 'APPROVED'), SUM(state = 'CHANGES_REQUESTED')"
            " FROM reviews WHERE submitted_at >= ? AND submitted_at < ?"
            f" AND {_in_scope('reviews', 'submitted_at')}"
            " GROUP BY reviewer, reviewer_type ORDER BY reviewer, reviewer_type",
            (low, high),
        )
        return [
            (login, account_type, reviews, approved, changes)
            for login, account_type, reviews, approved, changes in rows
        ]

    def reviewed_pull_requests(
        self, from_day: date, to_day: date
    ) -> list[tuple[str | None, str | None, str, int]]:
        """The distinct pull requests each reviewer reviewed, in scope.

        Rows are ``(login, type, repo, number)``, sorted, so the caller
        can count distinct pull requests across the forms of one login.
        """
        low, high = codec.day_bounds(from_day, to_day)
        rows = self._connection.execute(
            "SELECT DISTINCT reviewer, reviewer_type, repo, number FROM reviews"
            " WHERE submitted_at >= ? AND submitted_at < ?"
            f" AND {_in_scope('reviews', 'submitted_at')}"
            " ORDER BY reviewer, reviewer_type, repo, number",
            (low, high),
        )
        return [
            (login, account_type, repo, number)
            for login, account_type, repo, number in rows
        ]

    def run_actor_counts(self, from_day: date, to_day: date) -> list[ActorCount]:
        """Runs per triggering actor in the days, in scope, automation left out."""
        low, high = codec.day_bounds(from_day, to_day)
        rows = self._connection.execute(
            "SELECT actor, actor_type, COUNT(*) FROM runs"
            f" WHERE created_at >= ? AND created_at < ? AND NOT {_AUTOMATION}"
            f" AND {_in_scope('runs', 'created_at')}"
            " GROUP BY actor, actor_type ORDER BY actor, actor_type",
            (low, high, *AUTOMATION_EVENTS),
        )
        return [(login, account_type, count) for login, account_type, count in rows]

    def item_repository_counts(
        self, kind: str, column: str, from_day: date, to_day: date
    ) -> list[tuple[str, str | None, str | None, int]]:
        """Items of ``kind`` per repository and author by ``column``, in scope.

        Rows are ``(repo, login, type, count)``, sorted, so the caller can
        leave out ignored logins as the series do.
        """
        if kind not in _ITEM_KINDS:
            raise ValueError(f"unknown item kind {kind!r}")
        if column not in _ITEM_DAY_COLUMNS:
            raise ValueError(f"cannot count items by {column!r}")
        low, high = codec.day_bounds(from_day, to_day)
        rows = self._connection.execute(
            "SELECT repo, author, author_type, COUNT(*) FROM items"
            f" WHERE kind = ? AND {column} >= ? AND {column} < ?"
            f" AND {_in_scope('items', column)}"
            " GROUP BY repo, author, author_type ORDER BY repo, author, author_type",
            (kind, low, high),
        )
        return [
            (repo, login, account_type, count)
            for repo, login, account_type, count in rows
        ]

    def run_repository_counts(
        self, from_day: date, to_day: date
    ) -> list[tuple[str, str | None, str | None, str | None, int, int]]:
        """Runs and summed jobs per repository and actor, in scope.

        Rows are ``(repo, login, type, automation, runs, jobs)``; a run
        of an automation event carries no login, as in `run_day_counts`.
        """
        low, high = codec.day_bounds(from_day, to_day)
        rows = self._connection.execute(
            "SELECT repo,"
            f" CASE WHEN {_AUTOMATION} THEN NULL ELSE actor END AS login,"
            f" CASE WHEN {_AUTOMATION} THEN NULL ELSE actor_type END AS login_type,"
            f" CASE WHEN {_AUTOMATION} THEN event END AS automation,"
            " COUNT(*), COALESCE(SUM(jobs), 0)"
            " FROM runs WHERE created_at >= ? AND created_at < ?"
            f" AND {_in_scope('runs', 'created_at')}"
            " GROUP BY repo, login, login_type, automation"
            " ORDER BY repo, login, login_type, automation",
            (*(AUTOMATION_EVENTS * 3), low, high),
        )
        return [
            (repo, login, account_type, automation, runs, jobs)
            for repo, login, account_type, automation, runs, jobs in rows
        ]

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
