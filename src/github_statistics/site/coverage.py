# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""How complete the site's figures are, in words (design §8.5).

Each charted metric gets a line counting its complete days of those it
can have; a total of a metric with a partial day reads as a lower
bound, and jobs say for how many runs they were counted. While some
repository's item sync is unfinished, the item metrics' lines say how
many, and their totals read as lower bounds (design §6.2).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..render.model import Coverage

if TYPE_CHECKING:
    from .build import SiteTarget

COVERAGE_LABELS = {
    "pull_requests": "Pull requests",
    "issues": "Issues",
    "workflows": "Workflows",
    "workflow_outcomes": "Workflow outcomes",
}
HORIZON_NOTE = (
    "GitHub lists workflow runs for about a year; earlier days show as no data."
)
_RUN_METRICS = ("workflows", "workflow_outcomes")
_ITEM_METRICS = ("pull_requests", "issues")


def days_text(count: int) -> str:
    """``1 day`` or ``<count> days``."""
    return f"{count} day" if count == 1 else f"{count} days"


def _before_horizon(site: SiteTarget, metric: str) -> int:
    """The days of the window before the runs horizon; 0 for item metrics."""
    horizon = site.runs_horizon
    if metric not in _RUN_METRICS or horizon is None:
        return 0
    return sum(day < horizon for day in site.series.days)


def figure(
    site: SiteTarget,
    metric: str,
    value: int,
    unit: str = "",
    *,
    jobs: bool = False,
    view: str = "all",
) -> str:
    """One total of ``metric`` in words, ``at least`` when it has a partial day.

    An item metric's total is ``at least`` too while some repository's
    item sync is unfinished, as it may mix two reads.

    A total of ``jobs``, while some concluded run has no job count, says
    for how many runs it was counted; in a view other than ``all`` it
    reads as a lower bound instead, as the run counts span every class.
    Once every such run is counted, a run left out of the total, such
    as an ignored login's, no longer makes jobs a lower bound: only a
    day lacking some repository's runs does.
    """
    amount = f"{value} {unit}".rstrip()
    counts = site.job_runs
    if jobs and counts and counts[0] < counts[1]:
        if view == "all":
            return f"{amount} (counted for {counts[0]} of {counts[1]} runs)"
        return f"at least {amount}"
    if jobs and counts and site.runs_partial is not None:
        return f"at least {amount}" if site.runs_partial else amount
    mixed = site.mid_sync and metric in _ITEM_METRICS
    if mixed or Coverage.PARTIAL in site.series.coverage.get(metric, ()):
        return f"at least {amount}"
    return amount


def coverage_lines(site: SiteTarget) -> list[tuple[str, str]]:
    """Per charted metric, its label and its complete days of those it can have.

    For a run metric the days before the runs horizon are left out of
    the count, and the line says how many and why. An item metric's line
    says how many repositories are mid-sync, if any.
    """
    lines = []
    for metric, grades in site.series.coverage.items():
        before = _before_horizon(site, metric)
        counted = grades[before:]
        complete = sum(grade is Coverage.COMPLETE for grade in counted)
        text = f"{complete} of {days_text(len(counted))} complete"
        if before:
            text += f" ({days_text(before)} before GitHub's run history)"
        if site.mid_sync and metric in _ITEM_METRICS:
            noun = "repository" if site.mid_sync == 1 else "repositories"
            text += f" ({site.mid_sync} {noun} mid-sync;"
            text += " their figures may mix two reads)"
        lines.append((COVERAGE_LABELS[metric], text))
    return lines


def coverage_note(metric: str, grades: Sequence[Coverage]) -> list[str]:
    """The coverage notes of one metric's section; none when every day is complete."""
    missing = sum(grade is Coverage.NONE for grade in grades)
    partial = sum(grade is Coverage.PARTIAL for grade in grades)
    lines = []
    if missing:
        lines.append(f"{days_text(missing)} of {len(grades)} without data.")
    if partial:
        lines.append(f"{days_text(partial)} of {len(grades)} with partial data.")
    if missing and metric in _RUN_METRICS:
        lines.append(HORIZON_NOTE)
    return lines
