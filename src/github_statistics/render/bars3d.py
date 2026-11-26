# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The 2.5D bar chart: two rows of bars, one bar per day per row.

Each bar is three flat polygons (right side, top, front) in an oblique
projection. The back row is drawn before the front row, both left to
right, so painter ordering alone hides the right pixels and no depth
buffer is needed. The larger value of each day always stands in the
back row, so the smaller value is always visible in front.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import matplotlib
import matplotlib.style
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.collections import PolyCollection
from matplotlib.colors import to_rgb
from matplotlib.figure import Figure

from .furniture import RGB, draw_legend, draw_peaks, draw_text, px_to_pt, shade
from .layout import (
    Geometry,
    Point,
    Polygon,
    faces,
    legend_box,
    place_peak_labels,
    plan_chart,
)
from .model import (
    Bar,
    ChartPlan,
    ChartSpec,
    Coverage,
    GapSpan,
    RenderedChart,
    Style,
)


def _draw_backdrop(ax: Axes, plan: ChartPlan, style: Style, geo: Geometry) -> None:
    """Draw the floor, the back wall, its gridlines and tick labels, and month furniture."""
    floor = [
        (geo.left, geo.bottom),
        (geo.right, geo.bottom),
        (geo.right + geo.floor_dx, geo.bottom + geo.floor_dy),
        (geo.left + geo.floor_dx, geo.bottom + geo.floor_dy),
    ]
    wall_left, wall_right = geo.left + geo.floor_dx, geo.right + geo.floor_dx
    wall_bottom = geo.bottom + geo.floor_dy
    wall = [
        (wall_left, wall_bottom),
        (wall_right, wall_bottom),
        (wall_right, geo.top),
        (wall_left, geo.top),
    ]
    ax.add_collection(
        PolyCollection(
            [floor, wall], facecolors=[style.floor, style.wall], linewidths=0, zorder=1
        )
    )
    scale = geo.scale(plan.y_max)
    grid = [
        [(wall_left, wall_bottom + t * scale), (wall_right, wall_bottom + t * scale)]
        for t in plan.ticks[1:]
    ]
    ax.add_collection(
        _lines(grid, style.gridline, px_to_pt(style, style.gridline_width_px), zorder=2)
    )
    for tick in plan.ticks:
        ax.text(
            geo.left - style.tick_label_gap,
            wall_bottom + tick * scale,
            f"{tick:,}",
            ha="right",
            va="center",
            fontsize=style.tick_pt,
            color=style.muted,
            zorder=9,
        )
    half_gap = (geo.pitch - geo.width) / 2
    separators = []
    for start in plan.month_starts:
        x = geo.left + start * geo.pitch - half_gap
        separators.append(
            [(x, geo.bottom), (x + geo.floor_dx, geo.bottom + geo.floor_dy)]
        )
    ax.add_collection(
        _lines(
            separators,
            style.separator,
            px_to_pt(style, style.separator_width_px),
            zorder=2,
        )
    )
    for label in plan.month_labels:
        ax.text(
            geo.left + label.centre * geo.pitch - half_gap,
            geo.bottom - style.month_label_gap,
            label.text,
            ha="center",
            va="top",
            fontsize=style.month_pt,
            color=style.muted,
            zorder=9,
        )


def _lines(
    segments: list[list[Point]], colour: str, width: float, zorder: float
) -> PolyCollection:
    """Draw open polylines as a collection of unfilled polygons."""
    return PolyCollection(
        segments,
        closed=False,
        facecolors="none",
        edgecolors=colour,
        linewidths=width,
        zorder=zorder,
    )


def _gap_colour(style: Style, coverage: Coverage) -> str:
    """The slab colour for a gap of ``coverage``."""
    return style.no_data if coverage is Coverage.NONE else style.partial_data


def _draw_slab(
    ax: Axes, span: GapSpan, style: Style, geo: Geometry, zorder: float
) -> None:
    """Draw a hatched slab, wall-high and floor-deep, over ``span``."""
    height = geo.top - geo.bottom - geo.floor_dy
    x0 = geo.left + span.start * geo.pitch
    width = (span.stop - span.start - 1) * geo.pitch + geo.width
    colour = _gap_colour(style, span.coverage)
    ax.add_collection(
        PolyCollection(
            list(faces(x0, geo.bottom, height, width, geo.floor_dx, geo.floor_dy)),
            facecolors=[(*to_rgb(colour), style.gap_alpha)],
            edgecolors=colour,
            hatch=style.gap_hatch,
            linewidths=px_to_pt(style, style.edge_width_px),
            zorder=zorder,
        )
    )


def _draw_gaps(ax: Axes, plan: ChartPlan, style: Style, geo: Geometry) -> int:
    """Draw a slab behind the bars over each no-data span; return the slab count.

    Partial spans are drawn among the bars by :func:`_draw_rows`.
    """
    spans = [span for span in plan.gaps if span.coverage is Coverage.NONE]
    for span in spans:
        _draw_slab(ax, span, style, geo, zorder=3)
    return len(spans)


def _bar_height(value: int, scale: float) -> float:
    """The pixel height of a bar of ``value``."""
    return value * scale


def _drawable(spec: ChartSpec, bars: Sequence[Bar]) -> list[Bar]:
    """The bars of a row that are drawn: days with any data and a positive value."""
    return [
        bar
        for bar in bars
        if bar.value > 0 and spec.coverage[bar.day] is not Coverage.NONE
    ]


def _draw_shadows(
    ax: Axes, spec: ChartSpec, plan: ChartPlan, style: Style, geo: Geometry
) -> None:
    """Cast a soft, translucent shadow on the floor to the right of each bar."""
    scale = geo.scale(plan.y_max)
    shadows: list[Polygon] = []
    for row, bars in ((1, plan.back), (0, plan.front)):
        for bar in _drawable(spec, bars):
            x0, y0 = geo.bar_origin(bar.day, row)
            length = min(
                style.shadow_ratio * _bar_height(bar.value, scale),
                style.shadow_max_pitch * geo.pitch,
            )
            x1 = x0 + geo.width
            shadows.append(
                [
                    (x1, y0),
                    (x1 + length, y0),
                    (x1 + length + geo.dx, y0 + geo.dy),
                    (x1 + geo.dx, y0 + geo.dy),
                ]
            )
    ax.add_collection(
        PolyCollection(shadows, facecolors=[style.shadow_rgba], linewidths=0, zorder=4)
    )


def _draw_rows(
    ax: Axes, spec: ChartSpec, plan: ChartPlan, style: Style, geo: Geometry
) -> tuple[int, int]:
    """Draw the bars and the partial slabs; return the polygon and slab counts.

    Each partial slab splits the days at its end: the back row, then the
    front row, of the days up to that end, then the slab, then the days
    after it. So the slab covers its own bars and those standing behind
    it, which show through as a lower bound, but no bar in front of it.
    """
    colours = {spec.first.key: spec.first.colour, spec.second.key: spec.second.colour}
    scale = geo.scale(plan.y_max)
    factors = (style.side_shade, style.top_shade, style.front_shade)
    partial = [span for span in plan.gaps if span.coverage is Coverage.PARTIAL]
    count = 0
    start = 0
    for index, stop in enumerate([*(span.stop for span in partial), len(spec.days)]):
        for row, bars in ((1, plan.back), (0, plan.front)):
            polygons: list[Polygon] = []
            fills: list[RGB] = []
            edges: list[RGB] = []
            for bar in _drawable(spec, bars[start:stop]):
                x0, y0 = geo.bar_origin(bar.day, row)
                base = colours[bar.series]
                height = _bar_height(bar.value, scale)
                for polygon, factor in zip(
                    faces(x0, y0, height, geo.width, geo.dx, geo.dy),
                    factors,
                    strict=True,
                ):
                    polygons.append(polygon)
                    fills.append(shade(base, factor))
                    edges.append(shade(base, style.edge_shade))
            ax.add_collection(
                PolyCollection(
                    polygons,
                    facecolors=fills,
                    edgecolors=edges,
                    linewidths=px_to_pt(style, style.edge_width_px),
                    zorder=5,
                )
            )
            count += len(polygons)
        if index < len(partial):
            _draw_slab(ax, partial[index], style, geo, zorder=5)
        start = stop
    return count, len(partial)


def render_chart(
    spec: ChartSpec, path: Path, *, style: Style = Style()
) -> RenderedChart:
    """Render ``spec`` to a PNG at ``path`` and report what was drawn.

    The file is always a PNG written at exactly ``path``; its suffix
    neither selects the format nor gains a ``.png``.

    Uses the Agg canvas directly, without ``pyplot``, so no global
    backend or figure state changes. Renders start from matplotlib's
    default settings, so a caller's rcParams or matplotlibrc (a tight
    ``savefig.bbox``, say) cannot change the output. Two renders of one
    spec and style produce identical bytes.
    """
    plan = plan_chart(spec, style)
    geo = Geometry.build(style, len(spec.days))
    legend = legend_box(plan.legend, style)
    labels = place_peak_labels(spec, plan, style, legend)
    width_in = style.canvas_width / style.reference_dpi
    height_in = style.canvas_height / style.reference_dpi
    with (
        matplotlib.style.context("default"),
        matplotlib.rc_context(
            {
                "font.family": style.font_family,
                "hatch.linewidth": style.hatch_width_pt,
                "savefig.bbox": "standard",
            }
        ),
    ):
        figure = Figure(
            figsize=(width_in, height_in), dpi=style.dpi, facecolor=style.background
        )
        FigureCanvasAgg(figure)
        ax = figure.add_axes((0.0, 0.0, 1.0, 1.0))
        ax.set_xlim(0, style.canvas_width)
        ax.set_ylim(0, style.canvas_height)
        ax.set_axis_off()
        _draw_backdrop(ax, plan, style, geo)
        slabs = _draw_gaps(ax, plan, style, geo)
        _draw_shadows(ax, spec, plan, style, geo)
        polygons, partial_slabs = _draw_rows(ax, spec, plan, style, geo)
        draw_peaks(ax, labels, style)
        draw_legend(ax, spec, plan, style)
        draw_text(ax, spec, style)
        path.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(
            path,
            format="png",
            dpi=style.dpi,
            facecolor=style.background,
            metadata={"Software": None},
        )
    width, height = (round(v) for v in figure.get_size_inches() * style.dpi)
    return RenderedChart(
        path=path,
        width=width,
        height=height,
        plan=plan,
        bar_polygons=polygons,
        gap_slabs=slabs + partial_slabs,
        legend=plan.legend,
        legend_box=legend,
        peak_labels=labels,
    )
