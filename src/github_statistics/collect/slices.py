# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Date ranges for listings read in ``created=A..B`` slices (design §6.4)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

SECOND = timedelta(seconds=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def midnight(day: date) -> datetime:
    """00:00 UTC on ``day``."""
    return datetime.combine(day, time(), tzinfo=UTC)


def first_full_day(at: datetime) -> date:
    """The first day that starts at or after ``at``."""
    day = at.astimezone(UTC).date()
    return day if midnight(day) == at else day + DAY


def timestamp(at: datetime) -> str:
    """``at`` as a GitHub search timestamp."""
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slice_below(cursor: datetime, floor: datetime, days: int) -> datetime:
    """The start of the ``days``-day slice ending just before ``cursor``.

    The slice starts at midnight, and no earlier than ``floor``.
    """
    day = (cursor - SECOND).date()
    return max(floor, midnight(day) - timedelta(days=days - 1))


def split(low: datetime, high: datetime) -> list[tuple[datetime, datetime]]:
    """Ranges covering ``low`` to ``high``: halves in whole days, then hours.

    Oldest first; empty when the range is an hour or less.
    """
    width = high - low + SECOND
    if width > DAY:
        middle = low + timedelta(days=max(1, width.days // 2))
        return [(low, middle - SECOND), (middle, high)]
    if width <= HOUR:
        return []
    pieces = []
    start = low
    while start <= high:
        pieces.append((start, min(start + HOUR - SECOND, high)))
        start += HOUR
    return pieces
