# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Chart layout: row assignment, ticks, gap spans, month labels and geometry.

Nothing here draws. :func:`plan_chart` turns a :class:`ChartSpec` into
a :class:`ChartPlan` that tests can check without inspecting pixels,
and :class:`Geometry` maps that plan to design-pixel positions.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from .model import (
    Bar,
    Box,
    ChartPlan,
    ChartSpec,
    Coverage,
    GapSpan,
    MonthLabel,
    Peak,
    PeakLabel,
    Style,
)

MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_NICE_STEPS = (1.0, 2.0, 2.5, 5.0)
_MONTH_STRIDES = (1, 2, 3, 4, 6, 12)
# Average advance of a DejaVu Sans glyph, as a fraction of the font size.
GLYPH_WIDTH = 0.62

# A legend swatch, and its label's offset from the swatch's front-bottom-left corner.
SWATCH_WIDTH = 32.0
SWATCH_HEIGHT = 40.0
SWATCH_DX = 16.0
SWATCH_DY = 10.0
LEGEND_TEXT_X = 72.0
LEGEND_TEXT_Y = 20.0
# Text height, glyph bottom to top, as a fraction of the font size.
LINE_HEIGHT = 1.2
# Vertical distance between stacked peak labels, as a fraction of the font size.
LABEL_STEP = 1.6

NO_DATA_LABEL = "no data"
PARTIAL_DATA_LABEL = "partial data"

Point = tuple[float, float]
Polygon = list[Point]


@dataclass(frozen=True)
class Geometry:
    """Pixel positions derived from a style and a day count."""

    left: float
    right: float
    bottom: float
    top: float
    pitch: float
    width: float
    dx: float
    dy: float
    floor_dx: float
    floor_dy: float

    @classmethod
    def build(cls, style: Style, days: int) -> Geometry:
        """Lay out the plot area for ``days`` bars per row."""
        left = style.margin_left
        right = style.canvas_width - style.margin_right
        pitch = (right - left) / days
        depth_pitch = min(pitch, style.max_depth_pitch)
        dx = style.depth_x * depth_pitch
        dy = style.depth_y * depth_pitch
        return cls(
            left=left,
            right=right,
            bottom=style.margin_bottom,
            top=style.canvas_height - style.margin_top,
            pitch=pitch,
            width=style.bar_width * pitch,
            dx=dx,
            dy=dy,
            floor_dx=style.floor_depth * dx,
            floor_dy=style.floor_depth * dy,
        )

    def scale(self, y_max: int) -> float:
        """Pixels per unit of value, mapping ``y_max`` to the top of the wall."""
        return (self.top - self.bottom - self.floor_dy) / y_max

    def bar_origin(self, day: int, row: int) -> Point:
        """The front-bottom-left corner of the bar of ``day`` in ``row``."""
        return (
            self.left + day * self.pitch + row * self.dx,
            self.bottom + row * self.dy,
        )


def nice_ticks(peak: int, target: int = 6) -> tuple[int, ...]:
    """Round tick values from zero to at least ``peak``, about ``target`` intervals apart.

    Steps come from 1, 2, 2.5 and 5 times a power of ten; 2.5 is used
    only where it yields whole numbers, since every value is a count.
    """
    peak = max(peak, 1)
    raw = peak / max(target, 1)
    exponent = 0
    while True:
        for multiple in _NICE_STEPS:
            step = multiple * 10**exponent
            if step >= raw and step.is_integer():
                top = math.ceil(peak / step) * int(step)
                return tuple(range(0, top + 1, int(step)))
        exponent += 1


def _assign(spec: ChartSpec) -> tuple[tuple[Bar, ...], tuple[Bar, ...]]:
    """Put the larger value of each day in the back row and the smaller in front."""
    back: list[Bar] = []
    front: list[Bar] = []
    first, second = spec.first, spec.second
    for day, (a, b) in enumerate(zip(first.values, second.values, strict=True)):
        if a > b or (a == b and spec.back_on_tie == first.key):
            back_series, front_series = first, second
        else:
            back_series, front_series = second, first
        back.append(Bar(day, back_series.key, back_series.values[day]))
        front.append(Bar(day, front_series.key, front_series.values[day]))
    return tuple(back), tuple(front)


def _gap_spans(coverage: Sequence[Coverage]) -> tuple[GapSpan, ...]:
    """Group consecutive gap days that share a coverage into spans."""
    spans: list[GapSpan] = []
    for day, state in enumerate(coverage):
        if state is Coverage.COMPLETE:
            continue
        if spans and spans[-1].stop == day and spans[-1].coverage is state:
            spans[-1] = GapSpan(spans[-1].start, day + 1, state)
        else:
            spans.append(GapSpan(day, day + 1, state))
    return tuple(spans)


def _months(days: Sequence[date]) -> list[tuple[int, int, date]]:
    """Return ``(start, stop, first_day)`` for each calendar month in ``days``."""
    months: list[tuple[int, int, date]] = []
    start = 0
    for index in range(1, len(days) + 1):
        if index == len(days) or days[index].month != days[start].month:
            months.append((start, index, days[start]))
            start = index
    return months


def _month_labels(
    days: Sequence[date], style: Style, pitch: float
) -> tuple[MonthLabel, ...]:
    """Centre a ``Mon YYYY`` label in each month, thinning them where they would collide."""
    months = _months(days)
    font_px = style.month_pt * style.reference_dpi / 72
    label_px = len("Mmm YYYY") * GLYPH_WIDTH * font_px
    clearance = label_px * 1.15
    stride = next((s for s in _MONTH_STRIDES if 30 * pitch * s >= clearance), 12)
    labels: list[MonthLabel] = []
    for start, stop, first in months:
        if (first.year * 12 + first.month - 1) % stride:
            continue
        centre = (start + stop) / 2
        if (stop - start) * pitch < label_px and len(months) > 1:
            continue
        if labels and (centre - labels[-1].centre) * pitch < clearance:
            continue
        labels.append(MonthLabel(centre, f"{MONTHS[first.month - 1]} {first.year}"))
    return tuple(labels)


def _peaks(spec: ChartSpec, back: Sequence[Bar]) -> tuple[Peak, ...]:
    """Find each series' highest covered day, earliest on a tie."""
    peaks: list[Peak] = []
    for series in (spec.first, spec.second):
        best: tuple[int, int] | None = None
        for day, value in enumerate(series.values):
            if spec.coverage[day] is not Coverage.COMPLETE or value <= 0:
                continue
            if best is None or value > best[1]:
                best = (day, value)
        if best is not None:
            row = 1 if back[best[0]].series == series.key else 0
            peaks.append(Peak(series.key, best[0], best[1], row))
    return tuple(peaks)


def plan_chart(spec: ChartSpec, style: Style = Style()) -> ChartPlan:
    """Compute rows, ticks, gap spans, month labels, peaks and legend for ``spec``."""
    back, front = _assign(spec)
    covered = [bar.value for bar in back if spec.coverage[bar.day] is Coverage.COMPLETE]
    ticks = nice_ticks(max(covered, default=0), style.target_ticks)
    gaps = _gap_spans(spec.coverage)
    geometry = Geometry.build(style, len(spec.days))
    legend = [spec.first.label, spec.second.label]
    kinds = {span.coverage for span in gaps}
    if Coverage.NONE in kinds:
        legend.append(NO_DATA_LABEL)
    if Coverage.PARTIAL in kinds:
        legend.append(PARTIAL_DATA_LABEL)
    return ChartPlan(
        back=back,
        front=front,
        ticks=ticks,
        gaps=gaps,
        month_starts=tuple(start for start, _, _ in _months(spec.days)[1:]),
        month_labels=_month_labels(spec.days, style, geometry.pitch),
        peaks=_peaks(spec, back),
        legend=tuple(legend),
    )


def faces(
    x0: float, y0: float, height: float, width: float, dx: float, dy: float
) -> tuple[Polygon, Polygon, Polygon]:
    """Return the right side, top and front polygons of one bar, in drawing order."""
    front = [(x0, y0), (x0 + width, y0), (x0 + width, y0 + height), (x0, y0 + height)]
    top = [
        (x0, y0 + height),
        (x0 + width, y0 + height),
        (x0 + width + dx, y0 + height + dy),
        (x0 + dx, y0 + height + dy),
    ]
    side = [
        (x0 + width, y0),
        (x0 + width + dx, y0 + dy),
        (x0 + width + dx, y0 + height + dy),
        (x0 + width, y0 + height),
    ]
    return side, top, front


def _font_px(points: float, style: Style) -> float:
    """The size in design pixels of a font of ``points``."""
    return points * style.reference_dpi / 72


def legend_box(legend: Sequence[str], style: Style) -> Box:
    """Bound the legend's swatches and labels, one entry per item of ``legend``."""
    font_px = _font_px(style.legend_pt, style)
    text_px = max((len(label) for label in legend), default=0) * GLYPH_WIDTH * font_px
    half_line = font_px * LINE_HEIGHT / 2
    bottom = style.legend_y - (len(legend) - 1) * style.legend_step
    return Box(
        style.legend_x,
        min(bottom, bottom + LEGEND_TEXT_Y - half_line),
        style.legend_x + max(SWATCH_WIDTH + SWATCH_DX, LEGEND_TEXT_X + text_px),
        style.legend_y + max(SWATCH_HEIGHT + SWATCH_DY, LEGEND_TEXT_Y + half_line),
    )


def _bars_top(
    spec: ChartSpec, plan: ChartPlan, geo: Geometry, x0: float, x1: float
) -> float:
    """The highest point of any drawn bar reaching into ``x0`` to ``x1``."""
    scale = geo.scale(plan.y_max)
    top = geo.bottom
    for row, bars in ((1, plan.back), (0, plan.front)):
        for bar in bars:
            if bar.value <= 0 or spec.coverage[bar.day] is not Coverage.COMPLETE:
                continue
            left, base = geo.bar_origin(bar.day, row)
            if left < x1 and x0 < left + geo.width + geo.dx:
                top = max(top, base + bar.value * scale + geo.dy)
    return top


def place_peak_labels(
    spec: ChartSpec, plan: ChartPlan, style: Style, legend: Box
) -> tuple[PeakLabel, ...]:
    """Place each series' peak label above its bar, clear of the legend and each other.

    A label stands above every bar beneath its width, so it is not
    printed over the bar behind its peak or over taller neighbours;
    labels on nearby days stack. A label that would reach into the
    legend moves left of it, raised to clear any bar beneath its new
    position.
    """
    geo = Geometry.build(style, len(spec.days))
    scale = geo.scale(plan.y_max)
    font_px = _font_px(style.annotation_pt, style)
    height = font_px * LINE_HEIGHT
    labels: list[PeakLabel] = []

    def stack(x: float, y: float, half: float) -> float:
        for other in labels:
            other_half = (other.box.x1 - other.box.x0) / 2
            if (
                abs(other.x - x) < half + other_half + font_px
                and abs(other.y - y) < font_px * LABEL_STEP
            ):
                y = other.y + font_px * LABEL_STEP
        return y

    for peak in plan.peaks:
        x0, y0 = geo.bar_origin(peak.day, 1)
        x = x0 + (geo.width + geo.dx) / 2
        y = y0 + plan.back[peak.day].value * scale + geo.dy + style.annotation_gap
        day = spec.days[peak.day]
        text = f"{peak.value:,} · {day.day} {MONTHS[day.month - 1]}"
        half = len(text) * GLYPH_WIDTH * font_px / 2
        x = min(max(x, geo.left + half), geo.right + geo.floor_dx - half)
        y = max(
            y, _bars_top(spec, plan, geo, x - half, x + half) + style.annotation_gap
        )
        y = stack(x, y, half)
        if Box(x - half, y, x + half, y + height).overlaps(legend):
            x = max(legend.x0 - font_px / 2 - half, geo.left + half)
            y = max(
                y,
                _bars_top(spec, plan, geo, x - half, x + half) + style.annotation_gap,
            )
            y = stack(x, y, half)
        labels.append(
            PeakLabel(peak.series, text, x, y, Box(x - half, y, x + half, y + height))
        )
    return tuple(labels)
