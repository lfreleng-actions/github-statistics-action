# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The charts of one target, from its daily series (design §8.4).

One chart per enabled metric and per view in ``classes``, written as
``<out>/<owner>/img/<metric>-<class>.png`` with one derivative per
width in ``render.derivatives``. Reads no store and no clock: the
generation date comes from the ``now`` argument, so two renders on one
day give identical files.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from github_statistics import __version__

from ..store import path_component
from .bars3d import render_chart
from .images import write_derivatives
from .model import ChartSpec, Coverage, RenderedChart, Series, Style
from .palette import series_colour

if TYPE_CHECKING:
    from ..config import Config, Target
    from ..series import TargetSeries

_GLOB = frozenset("*?[")


@dataclass(frozen=True)
class _Metric:
    """How one metric's chart is titled and which two series it draws."""

    name: str
    title: str
    first: str
    second: str
    back_on_tie: str


_METRICS = (
    _Metric("pull_requests", "pull requests per day", "opened", "closed", "closed"),
    _Metric("issues", "issues per day", "opened", "closed", "closed"),
    _Metric("workflows", "workflow runs and jobs per day", "runs", "jobs", "jobs"),
    _Metric(
        "workflow_outcomes",
        "workflow outcomes per day",
        "succeeded",
        "failed",
        "failed",
    ),
)

VIEW_LABELS = {
    "all": "everyone",
    "member": "members",
    "external": "external contributors",
    "org_bot": "our bots",
    "external_bot": "external bots",
    "humans": "humans",
    "bots": "bots",
}


@dataclass(frozen=True)
class TargetChart:
    """One chart of a target: what it showed and the files it wrote."""

    metric: str
    view: str
    spec: ChartSpec
    chart: RenderedChart
    derivatives: tuple[Path, ...]

    @property
    def paths(self) -> tuple[Path, ...]:
        """The master, then each derivative."""
        return (self.chart.path, *self.derivatives)


def _style(width: int, height: int) -> Style:
    """The default style rendered at ``width`` by ``height`` pixels.

    The renderer's geometry is fixed on its design canvas, so only the
    raster size changes, and only by a whole number of dots per inch.
    """
    base = Style()
    dpi, remainder = divmod(base.reference_dpi * width, base.canvas_width)
    if remainder or width * base.canvas_height != height * base.canvas_width or not dpi:
        raise ValueError(
            f"render size {width}x{height} must be a multiple of"
            f" {base.canvas_width // base.reference_dpi}x"
            f"{base.canvas_height // base.reference_dpi}"
        )
    return Style(dpi=dpi)


def _subject(target: Target) -> str:
    """The owner, or ``owner/repo`` for a target that selects one repository."""
    if len(target.include) == 1 and not _GLOB & set(target.include[0]):
        return f"{target.owner}/{target.include[0]}"
    return target.owner


def _count(count: int, one: str, many: str) -> str:
    """``1 <one>`` or ``<count> <many>``."""
    return f"{count} {one if count == 1 else many}"


def _days(count: int) -> str:
    """``1 day`` or ``<count> days``."""
    return _count(count, "day", "days")


def _footer(coverage: Sequence[Coverage], now: datetime) -> str:
    """Coverage in words, the tool version and the generation date."""
    complete = sum(day is Coverage.COMPLETE for day in coverage)
    missing = sum(day is Coverage.NONE for day in coverage)
    parts = [f"Coverage: {complete} of {_days(len(coverage))} complete"]
    if missing:
        parts.append(f"{_days(missing)} without data")
    parts.append(f"github-statistics {__version__}")
    parts.append(f"generated {now.date().isoformat()}")
    return " · ".join(parts)


def _series(
    series: TargetSeries, config: Config, metric: str, key: str, view: str
) -> Series:
    """One series of a chart, coloured by the palette or its override."""
    return Series(
        key=key,
        label=key.capitalize(),
        colour=config.render.palette.get(key) or series_colour(key),
        values=series.values(metric, key, view),
    )


def render_target(
    series: TargetSeries,
    config: Config,
    target: Target,
    out_dir: Path,
    *,
    now: datetime,
) -> list[TargetChart]:
    """Render every chart of ``target`` under ``out_dir`` and return them in order.

    Metrics run in a fixed order (pull requests, issues, workflows,
    workflow outcomes), each over the views in ``config.classes``. A
    metric off in ``config.metrics`` renders nothing; a view with no
    events still renders. ``render.svg`` raises `ValueError` before any
    file is written: the renderer draws PNG alone, and a partial output
    must not pass for the one configured. So does a width in
    ``render.derivatives`` outside ``1..render.width``, which would
    otherwise fail after the first charts were written.
    """
    if config.render.svg:
        raise ValueError(
            "render.svg is not supported yet: the renderer writes PNG only;"
            " turn render.svg off"
        )
    style = _style(config.render.width, config.render.height)
    oversized = [
        width
        for width in config.render.derivatives
        if not 0 < width <= config.render.width
    ]
    if oversized:
        raise ValueError(
            f"render.derivatives {', '.join(map(str, oversized))} must lie"
            f" within 1..{config.render.width}, the render.width of the master"
        )
    images = Path(out_dir) / path_component(target.owner, "owner") / "img"
    subject = _subject(target)
    window = f"{_days(len(series.days))} to {series.days[-1].isoformat()} (UTC)"
    repositories = _count(series.repositories[-1], "repository", "repositories")
    charts: list[TargetChart] = []
    for metric in _METRICS:
        if not getattr(config.metrics, metric.name):
            continue
        coverage = series.coverage[metric.name]
        footer = _footer(coverage, now)
        back = getattr(config.render.back_on_tie, metric.name, metric.back_on_tie)
        for view in config.classes:
            spec = ChartSpec(
                title=f"{subject} — {metric.title}",
                subtitle=" · ".join(
                    (
                        window,
                        repositories,
                        VIEW_LABELS[view],
                        f"{metric.first} vs {metric.second}",
                    )
                ),
                footer=footer,
                days=series.days,
                first=_series(series, config, metric.name, metric.first, view),
                second=_series(series, config, metric.name, metric.second, view),
                coverage=coverage,
                back_on_tie=back,
            )
            stem = f"{metric.name}-{view}".replace("_", "-")
            chart = render_chart(spec, images / f"{stem}.png", style=style)
            derivatives = write_derivatives(chart.path, config.render.derivatives)
            charts.append(
                TargetChart(metric.name, view, spec, chart, tuple(derivatives))
            )
    return charts
