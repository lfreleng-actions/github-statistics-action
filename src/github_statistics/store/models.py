# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Typed records the store reads and writes (design §6.1).

Timestamps are timezone-aware `datetime` values and days are `date`
values; the codec module turns both into ISO-8601 UTC text for SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Item:
    """One issue or pull request; the ``*_by`` fields stay ``None`` until enriched."""

    repo: str
    number: int
    node_id: str
    kind: str
    created_at: datetime
    closed_at: datetime | None
    merged_at: datetime | None
    author: str | None
    author_type: str | None
    updated_at: datetime
    merged_by: str | None = None
    merged_by_type: str | None = None
    closed_by: str | None = None
    closed_by_type: str | None = None
    reviews_read_at: datetime | None = None


@dataclass(frozen=True)
class Enrichment:
    """What one GraphQL read adds to a stored item (design §6.3)."""

    repo: str
    number: int
    merged_by: str | None
    merged_by_type: str | None
    closed_by: str | None
    closed_by_type: str | None
    read_at: datetime


@dataclass(frozen=True)
class Review:
    """One submitted pull request review."""

    id: int
    node_id: str
    repo: str
    number: int
    reviewer: str | None
    reviewer_type: str | None
    state: str
    submitted_at: datetime | None


@dataclass(frozen=True)
class Run:
    """One workflow run; ``jobs`` stays ``None`` until enriched (design §6.4)."""

    id: int
    node_id: str
    repo: str
    workflow_path: str
    event: str
    status: str
    conclusion: str | None
    created_at: datetime
    actor: str | None
    actor_type: str | None
    attempt: int
    jobs: int | None = None


@dataclass(frozen=True)
class Member:
    """One public member of an organisation, and when the tool saw them."""

    org: str
    login: str
    first_seen: date
    last_seen: date


@dataclass(frozen=True)
class SyncState:
    """How far the sync of one metric of one repository got (design §6.5)."""

    repo: str
    metric: str
    watermark: datetime | None = None
    etag: str | None = None
    oldest: datetime | None = None
    last_sync_at: datetime | None = None
    swept_to: datetime | None = None


@dataclass(frozen=True)
class DayRange:
    """A closed range of days, both ends included."""

    from_day: date
    to_day: date


@dataclass(frozen=True)
class ActorDayCount:
    """How many events one actor made on one day, from an aggregate query."""

    day: date
    login: str | None
    account_type: str | None
    count: int


@dataclass(frozen=True)
class RunDayCount:
    """Runs and their jobs on one day, for one actor, event kind and conclusion.

    A scheduled run carries no ``login`` or ``account_type``: it is
    credited to no one (design §7.3).
    """

    day: date
    login: str | None
    account_type: str | None
    scheduled: bool
    conclusion: str | None
    runs: int
    jobs: int
