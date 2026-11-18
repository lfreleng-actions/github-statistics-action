# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Conversion between the store's records and SQLite rows.

Timestamps are stored as ISO-8601 UTC text (``2026-03-01T12:00:00Z``)
and days as ``YYYY-MM-DD``, so text comparison in SQL orders them
correctly.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .models import Item, Member, Review, Run, SyncState

_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def timestamp(value: datetime | None) -> str | None:
    """``value`` as ISO-8601 UTC text; a naive or non-datetime value is an error."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"expected a datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).strftime(_FORMAT)


def parse_timestamp(text: str | None) -> datetime | None:
    """The UTC `datetime` that `timestamp` wrote as ``text``."""
    if text is None:
        return None
    return datetime.strptime(text, _FORMAT).replace(tzinfo=UTC)


def _required(text: str) -> datetime:
    """`parse_timestamp` for a column that is never ``NULL``."""
    return datetime.strptime(text, _FORMAT).replace(tzinfo=UTC)


def day(value: date) -> str:
    """``value`` as ``YYYY-MM-DD``; a `datetime` is not a day."""
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"expected a date, got {type(value).__name__}")
    return value.isoformat()


def parse_day(text: str) -> date:
    """The `date` that `day` wrote as ``text``."""
    return date.fromisoformat(text)


def day_bounds(from_day: date, to_day: date) -> tuple[str, str]:
    """Timestamp text bounding the days ``from_day`` to ``to_day`` inclusive.

    A timestamp ``t`` falls in the range when ``low <= t < high``.
    """
    low = f"{day(from_day)}T00:00:00Z"
    high = f"{day(to_day + timedelta(days=1))}T00:00:00Z"
    return low, high


ITEM_COLUMNS = (
    "repo",
    "number",
    "node_id",
    "kind",
    "created_at",
    "closed_at",
    "merged_at",
    "author",
    "author_type",
    "merged_by",
    "merged_by_type",
    "closed_by",
    "closed_by_type",
    "updated_at",
    "reviews_read_at",
)


def item_row(item: Item) -> tuple[Any, ...]:
    """``item`` as a row in `ITEM_COLUMNS` order."""
    return (
        item.repo,
        item.number,
        item.node_id,
        item.kind,
        timestamp(item.created_at),
        timestamp(item.closed_at),
        timestamp(item.merged_at),
        item.author,
        item.author_type,
        item.merged_by,
        item.merged_by_type,
        item.closed_by,
        item.closed_by_type,
        timestamp(item.updated_at),
        timestamp(item.reviews_read_at),
    )


def item_from_row(row: sqlite3.Row) -> Item:
    """The `Item` stored in ``row``."""
    return Item(
        repo=row["repo"],
        number=row["number"],
        node_id=row["node_id"],
        kind=row["kind"],
        created_at=_required(row["created_at"]),
        closed_at=parse_timestamp(row["closed_at"]),
        merged_at=parse_timestamp(row["merged_at"]),
        author=row["author"],
        author_type=row["author_type"],
        updated_at=_required(row["updated_at"]),
        merged_by=row["merged_by"],
        merged_by_type=row["merged_by_type"],
        closed_by=row["closed_by"],
        closed_by_type=row["closed_by_type"],
        reviews_read_at=parse_timestamp(row["reviews_read_at"]),
    )


REVIEW_COLUMNS = (
    "id",
    "node_id",
    "repo",
    "number",
    "reviewer",
    "reviewer_type",
    "state",
    "submitted_at",
)


def review_row(review: Review) -> tuple[Any, ...]:
    """``review`` as a row in `REVIEW_COLUMNS` order."""
    return (
        review.id,
        review.node_id,
        review.repo,
        review.number,
        review.reviewer,
        review.reviewer_type,
        review.state,
        timestamp(review.submitted_at),
    )


def review_from_row(row: sqlite3.Row) -> Review:
    """The `Review` stored in ``row``."""
    return Review(
        id=row["id"],
        node_id=row["node_id"],
        repo=row["repo"],
        number=row["number"],
        reviewer=row["reviewer"],
        reviewer_type=row["reviewer_type"],
        state=row["state"],
        submitted_at=parse_timestamp(row["submitted_at"]),
    )


RUN_COLUMNS = (
    "id",
    "node_id",
    "repo",
    "workflow_path",
    "event",
    "status",
    "conclusion",
    "created_at",
    "actor",
    "actor_type",
    "attempt",
    "jobs",
)


def run_row(run: Run) -> tuple[Any, ...]:
    """``run`` as a row in `RUN_COLUMNS` order."""
    return (
        run.id,
        run.node_id,
        run.repo,
        run.workflow_path,
        run.event,
        run.status,
        run.conclusion,
        timestamp(run.created_at),
        run.actor,
        run.actor_type,
        run.attempt,
        run.jobs,
    )


def run_from_row(row: sqlite3.Row) -> Run:
    """The `Run` stored in ``row``."""
    return Run(
        id=row["id"],
        node_id=row["node_id"],
        repo=row["repo"],
        workflow_path=row["workflow_path"],
        event=row["event"],
        status=row["status"],
        conclusion=row["conclusion"],
        created_at=_required(row["created_at"]),
        actor=row["actor"],
        actor_type=row["actor_type"],
        attempt=row["attempt"],
        jobs=row["jobs"],
    )


def member_from_row(row: sqlite3.Row) -> Member:
    """The `Member` stored in ``row``."""
    return Member(
        org=row["org"],
        login=row["login"],
        first_seen=parse_day(row["first_seen"]),
        last_seen=parse_day(row["last_seen"]),
    )


SYNC_COLUMNS = (
    "repo",
    "metric",
    "watermark",
    "etag",
    "oldest",
    "last_sync_at",
    "swept_to",
)


def sync_row(state: SyncState) -> tuple[Any, ...]:
    """``state`` as a row in `SYNC_COLUMNS` order."""
    return (
        state.repo,
        state.metric,
        timestamp(state.watermark),
        state.etag,
        timestamp(state.oldest),
        timestamp(state.last_sync_at),
        timestamp(state.swept_to),
    )


def sync_from_row(row: sqlite3.Row) -> SyncState:
    """The `SyncState` stored in ``row``."""
    return SyncState(
        repo=row["repo"],
        metric=row["metric"],
        watermark=parse_timestamp(row["watermark"]),
        etag=row["etag"],
        oldest=parse_timestamp(row["oldest"]),
        last_sync_at=parse_timestamp(row["last_sync_at"]),
        swept_to=parse_timestamp(row["swept_to"]),
    )
