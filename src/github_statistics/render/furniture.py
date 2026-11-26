# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Chart furniture: bar shading, peak labels, the legend and the text.

:mod:`.bars3d` draws the backdrop, the gaps and the bars, then calls
these to finish a chart.
"""

from __future__ import annotations

from collections.abc import Sequence

from matplotlib.axes import Axes
from matplotlib.collections import PolyCollection
from matplotlib.colors import to_rgb

from .layout import (
    LEGEND_TEXT_X,
    LEGEND_TEXT_Y,
    NO_DATA_LABEL,
    PARTIAL_DATA_LABEL,
    SWATCH_DX,
    SWATCH_DY,
    SWATCH_HEIGHT,
    SWATCH_WIDTH,
    faces,
)
from .model import ChartPlan, ChartSpec, PeakLabel, Style

RGB = tuple[float, float, float]


def shade(colour: str, factor: float) -> RGB:
    """Scale the channels of ``colour`` by ``factor``, clamped to 1."""
    red, green, blue = to_rgb(colour)
    return (min(red * factor, 1.0), min(green * factor, 1.0), min(blue * factor, 1.0))


def px_to_pt(style: Style, pixels: float) -> float:
    """Convert design pixels to points, so line widths scale with ``dpi``."""
    return pixels * 72 / style.reference_dpi


def draw_peaks(ax: Axes, labels: Sequence[PeakLabel], style: Style) -> None:
    """Draw each series' peak label at its planned position."""
    for label in labels:
        ax.text(
            label.x,
            label.y,
            label.text,
            ha="center",
            va="bottom",
            fontsize=style.annotation_pt,
            color=style.text,
            zorder=9,
        )


def _swatch(
    ax: Axes, x: float, y: float, colour: str, style: Style, *, hatch: str | None = None
) -> None:
    """Draw a small 3D bar as a legend swatch with its front-bottom-left corner at ``(x, y)``."""
    polygons = list(faces(x, y, SWATCH_HEIGHT, SWATCH_WIDTH, SWATCH_DX, SWATCH_DY))
    if hatch is None:
        fills = [
            shade(colour, f)
            for f in (style.side_shade, style.top_shade, style.front_shade)
        ]
        ax.add_collection(
            PolyCollection(
                polygons,
                facecolors=fills,
                edgecolors=[shade(colour, style.edge_shade)],
                linewidths=px_to_pt(style, style.edge_width_px),
                zorder=9,
            )
        )
    else:
        ax.add_collection(
            PolyCollection(
                polygons,
                facecolors=[(*to_rgb(colour), style.gap_alpha)],
                edgecolors=colour,
                hatch=hatch,
                linewidths=px_to_pt(style, style.edge_width_px),
                zorder=9,
            )
        )


def draw_legend(ax: Axes, spec: ChartSpec, plan: ChartPlan, style: Style) -> None:
    """List the series and any gap kinds at the top right, each with a 3D swatch."""
    entries: list[tuple[str, str, str | None]] = [
        (spec.first.label, spec.first.colour, None),
        (spec.second.label, spec.second.colour, None),
    ]
    if NO_DATA_LABEL in plan.legend:
        entries.append((NO_DATA_LABEL, style.no_data, style.gap_hatch))
    if PARTIAL_DATA_LABEL in plan.legend:
        entries.append((PARTIAL_DATA_LABEL, style.partial_data, style.gap_hatch))
    for index, (label, colour, hatch) in enumerate(entries):
        y = style.legend_y - index * style.legend_step
        _swatch(ax, style.legend_x, y, colour, style, hatch=hatch)
        ax.text(
            style.legend_x + LEGEND_TEXT_X,
            y + LEGEND_TEXT_Y,
            label,
            ha="left",
            va="center",
            fontsize=style.legend_pt,
            color=style.text,
            zorder=9,
        )


def draw_text(ax: Axes, spec: ChartSpec, style: Style) -> None:
    """Place the title, subtitle and footer."""
    ax.text(
        style.text_x,
        style.title_y,
        spec.title,
        ha="left",
        va="top",
        fontsize=style.title_pt,
        fontweight="bold",
        color=style.text,
    )
    ax.text(
        style.text_x,
        style.subtitle_y,
        spec.subtitle,
        ha="left",
        va="top",
        fontsize=style.subtitle_pt,
        color=style.muted,
    )
    ax.text(
        style.text_x,
        style.footer_y,
        spec.footer,
        ha="left",
        va="bottom",
        fontsize=style.footer_pt,
        color=style.muted,
    )
