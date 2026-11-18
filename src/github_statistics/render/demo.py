# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Synthetic chart specifications for the ``demo`` command.

The data imitates a busy organisation: weekly seasonality, a slow
upward drift, two bulk-merge spikes, four days with no data and one
with partial data. It needs no token and no store.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta

from .model import ChartSpec, Coverage, Series
from .palette import series_colour

# Monday first: weekends are quiet.
_WEEKLY = (1.0, 1.08, 1.05, 1.0, 0.86, 0.3, 0.22)
_GAP_DAYS = 4
_PARTIAL_DAYS = 1
_SPIKES = 2
_SPIKE_FACTOR = 6.0
MIN_DAYS = _SPIKES + _GAP_DAYS + _PARTIAL_DAYS


@dataclass(frozen=True)
class _Line:
    """One generated series: its key, legend text and mean daily count."""

    key: str
    label: str
    level: float
    spikes: bool


@dataclass(frozen=True)
class _Metric:
    """How one demo chart's two series are generated."""

    name: str
    title: str
    first: _Line
    second: _Line
    back_on_tie: str


_METRICS = (
    _Metric(
        "pull-requests",
        "Pull requests opened and closed",
        _Line("opened", "Opened", 24.0, spikes=False),
        _Line("closed", "Closed", 22.0, spikes=True),
        "closed",
    ),
    _Metric(
        "issues",
        "Issues opened and closed",
        _Line("opened", "Opened", 9.0, spikes=False),
        _Line("closed", "Closed", 8.0, spikes=True),
        "closed",
    ),
    _Metric(
        "workflows",
        "Workflow runs and jobs",
        _Line("runs", "Runs", 110.0, spikes=True),
        _Line("jobs", "Jobs", 330.0, spikes=True),
        "jobs",
    ),
)


def _series(
    rng: random.Random, line: _Line, days: tuple[date, ...], spikes: set[int]
) -> Series:
    """Generate one series with weekly seasonality, a slow drift and optional spikes."""
    count = len(days)
    values = []
    for index, day in enumerate(days):
        mean = (
            line.level
            * _WEEKLY[day.weekday()]
            * (0.8 + 0.45 * index / max(count - 1, 1))
        )
        if line.spikes and index in spikes:
            mean *= _SPIKE_FACTOR
        values.append(max(0, round(rng.gauss(mean, mean**0.5))))
    return Series(
        key=line.key,
        label=line.label,
        colour=series_colour(line.key),
        values=tuple(values),
    )


def demo_specs(days: int, seed: int, until: date) -> list[tuple[str, ChartSpec]]:
    """Return ``(name, spec)`` for each demo chart over ``days`` days ending on ``until``.

    The same ``days``, ``seed`` and ``until`` always give the same data.
    """
    if days < MIN_DAYS:
        raise ValueError(f"the demo needs at least {MIN_DAYS} days")
    rng = random.Random(seed)
    window = tuple(until - timedelta(days=offset) for offset in range(days - 1, -1, -1))
    picked = rng.sample(range(days), MIN_DAYS)
    spikes = set(picked[:_SPIKES])
    gaps = set(picked[_SPIKES : _SPIKES + _GAP_DAYS])
    partial = set(picked[_SPIKES + _GAP_DAYS :])
    coverage = tuple(
        Coverage.NONE
        if index in gaps
        else Coverage.PARTIAL
        if index in partial
        else Coverage.COMPLETE
        for index in range(days)
    )
    subtitle = f"demo-org · synthetic data, seed {seed} · {days} days to {until.isoformat()} · all actors"
    footer = f"Synthetic demo data · {_GAP_DAYS} days without data, {_PARTIAL_DAYS} partial · github-statistics demo"
    specs = []
    for metric in _METRICS:
        spec = ChartSpec(
            title=metric.title,
            subtitle=subtitle,
            footer=footer,
            days=window,
            first=_series(rng, metric.first, window, spikes),
            second=_series(rng, metric.second, window, spikes),
            coverage=coverage,
            back_on_tie=metric.back_on_tie,
        )
        specs.append((metric.name, spec))
    return specs
