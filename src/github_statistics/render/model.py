# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Plain data the chart renderer draws from, and the style it draws with.

Everything here is a frozen dataclass: the renderer reads no store, no
network and no global state, so a chart is a pure function of its
:class:`ChartSpec` and :class:`Style`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path

from . import palette

_HEX_COLOUR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class Coverage(StrEnum):
    """How completely the store covers one day."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    NONE = "none"


@dataclass(frozen=True)
class Series:
    """One row of bars: a daily count with its legend text and colour."""

    key: str
    label: str
    colour: str
    values: tuple[int, ...]

    def __post_init__(self) -> None:
        """Reject a malformed colour or a negative count."""
        if not _HEX_COLOUR.match(self.colour):
            raise ValueError(f"series {self.key!r}: colour must be #RRGGBB")
        if any(value < 0 for value in self.values):
            raise ValueError(f"series {self.key!r}: values must not be negative")


@dataclass(frozen=True)
class ChartSpec:
    """Everything one chart shows: two series over consecutive UTC days."""

    title: str
    subtitle: str
    footer: str
    days: tuple[date, ...]
    first: Series
    second: Series
    coverage: tuple[Coverage, ...]
    back_on_tie: str

    def __post_init__(self) -> None:
        """Check that the days, values and coverage line up."""
        count = len(self.days)
        if count == 0:
            raise ValueError("a chart needs at least one day")
        for name, length in (
            (self.first.key, len(self.first.values)),
            (self.second.key, len(self.second.values)),
            ("coverage", len(self.coverage)),
        ):
            if length != count:
                raise ValueError(f"{name}: {length} entries for {count} days")
        if self.first.key == self.second.key:
            raise ValueError("the two series need distinct keys")
        if self.back_on_tie not in (self.first.key, self.second.key):
            raise ValueError(f"back_on_tie {self.back_on_tie!r} names neither series")
        one_day = timedelta(days=1)
        for earlier, later in zip(self.days, self.days[1:], strict=False):
            if later - earlier != one_day:
                raise ValueError(f"days are not consecutive at {later.isoformat()}")


@dataclass(frozen=True)
class Style:
    """Every tunable number and colour of the chart.

    Geometry is in design pixels on a ``canvas_width`` by
    ``canvas_height`` canvas. ``dpi`` sets only the raster size: the
    master is 3840x2160 at 240 dpi, and a lower ``dpi`` renders the same
    picture smaller.
    """

    canvas_width: int = 3840
    canvas_height: int = 2160
    reference_dpi: int = 240
    dpi: int = 240

    margin_left: float = 280.0
    margin_right: float = 210.0
    margin_bottom: float = 300.0
    margin_top: float = 350.0

    bar_width: float = 0.62
    depth_x: float = 0.75
    depth_y: float = 0.45
    # Short windows have a wide pitch; capping the pitch the depth vector
    # follows keeps the floor and wall inside the right margin.
    max_depth_pitch: float = 64.0
    floor_depth: float = 2.8
    target_ticks: int = 6

    side_shade: float = 0.74
    top_shade: float = 1.22
    front_shade: float = 1.0
    edge_shade: float = 0.62
    edge_width_px: float = 0.5

    shadow_rgba: tuple[float, float, float, float] = (0.1, 0.12, 0.18, 0.16)
    shadow_ratio: float = 0.09
    shadow_max_pitch: float = 2.5

    gap_hatch: str = "////"
    gap_alpha: float = 0.32
    hatch_width_pt: float = 0.6
    gridline_width_px: float = 2.0
    separator_width_px: float = 2.0

    text_x: float = 120.0
    title_y: float = 2070.0
    subtitle_y: float = 1960.0
    footer_y: float = 70.0
    legend_x: float = 3200.0
    legend_y: float = 2010.0
    legend_step: float = 64.0
    month_label_gap: float = 26.0
    tick_label_gap: float = 16.0
    annotation_gap: float = 40.0

    title_pt: float = 20.0
    subtitle_pt: float = 9.0
    footer_pt: float = 7.0
    legend_pt: float = 9.0
    month_pt: float = 8.0
    tick_pt: float = 8.0
    annotation_pt: float = 8.0

    background: str = palette.BACKGROUND
    floor: str = palette.FLOOR
    wall: str = palette.WALL
    gridline: str = palette.GRIDLINE
    separator: str = palette.SEPARATOR
    text: str = palette.TEXT
    muted: str = palette.MUTED
    no_data: str = palette.NO_DATA
    partial_data: str = palette.PARTIAL_DATA
    font_family: str = "DejaVu Sans"


@dataclass(frozen=True)
class Bar:
    """One day's value of one series, placed in the front or back row."""

    day: int
    series: str
    value: int


@dataclass(frozen=True)
class GapSpan:
    """A run of consecutive days, ``start`` to ``stop`` exclusive, sharing a gap coverage."""

    start: int
    stop: int
    coverage: Coverage


@dataclass(frozen=True)
class MonthLabel:
    """A ``Mon YYYY`` label centred at ``centre``, measured in days from the first bar."""

    centre: float
    text: str


@dataclass(frozen=True)
class Peak:
    """The highest day of a series and the row its bar stands in (1 is back)."""

    series: str
    day: int
    value: int
    row: int


@dataclass(frozen=True)
class Box:
    """An axis-aligned rectangle in design pixels, ``x0 <= x1`` and ``y0 <= y1``."""

    x0: float
    y0: float
    x1: float
    y1: float

    def overlaps(self, other: Box) -> bool:
        """Whether this box and ``other`` share any area."""
        return (
            self.x0 < other.x1
            and other.x0 < self.x1
            and self.y0 < other.y1
            and other.y0 < self.y1
        )


@dataclass(frozen=True)
class PeakLabel:
    """A peak annotation: its text, bottom-centre anchor and estimated bounds."""

    series: str
    text: str
    x: float
    y: float
    box: Box


@dataclass(frozen=True)
class ChartPlan:
    """The computed layout of a chart, independent of pixels.

    ``back`` and ``front`` hold one bar per day, gap days included;
    the renderer skips days without data and zero values when drawing.
    """

    back: tuple[Bar, ...]
    front: tuple[Bar, ...]
    ticks: tuple[int, ...]
    gaps: tuple[GapSpan, ...]
    month_starts: tuple[int, ...]
    month_labels: tuple[MonthLabel, ...]
    peaks: tuple[Peak, ...]
    legend: tuple[str, ...]

    @property
    def y_max(self) -> int:
        """The top tick, which the vertical scale maps to the wall's top."""
        return self.ticks[-1]


@dataclass(frozen=True)
class RenderedChart:
    """What :func:`render_chart` wrote and what it drew."""

    path: Path
    width: int
    height: int
    plan: ChartPlan
    bar_polygons: int
    gap_slabs: int
    legend: tuple[str, ...]
    legend_box: Box
    peak_labels: tuple[PeakLabel, ...]
