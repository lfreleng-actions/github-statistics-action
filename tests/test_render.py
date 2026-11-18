# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the 2.5D chart renderer."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

START = date(2026, 1, 1)
SMALL_DPI = 24


def _spec(
    first: list[int],
    second: list[int],
    *,
    coverage: list[str] | None = None,
    back_on_tie: str = "closed",
) -> Any:
    """Build a chart spec over consecutive days from two value lists."""
    from github_statistics.render import ChartSpec, Coverage, Series

    count = len(first)
    states = coverage or ["complete"] * count
    return ChartSpec(
        title="Pull requests",
        subtitle="test",
        footer="footer",
        days=tuple(START + timedelta(days=i) for i in range(count)),
        first=Series("opened", "Opened", "#0072B2", tuple(first)),
        second=Series("closed", "Closed", "#E69F00", tuple(second)),
        coverage=tuple(Coverage(state) for state in states),
        back_on_tie=back_on_tie,
    )


def _small_style() -> Any:
    """A style that renders the master's picture at a tenth of its size."""
    from github_statistics.render import Style

    return Style(dpi=SMALL_DPI)


_pairs = st.integers(min_value=1, max_value=60).flatmap(
    lambda n: st.tuples(
        st.lists(st.integers(0, 10_000), min_size=n, max_size=n),
        st.lists(st.integers(0, 10_000), min_size=n, max_size=n),
        st.sampled_from(["opened", "closed"]),
    )
)


@given(_pairs)
def test_row_assignment_puts_the_larger_value_behind(
    case: tuple[list[int], list[int], str],
) -> None:
    """The back value is never smaller, each series appears once per day, ties follow back_on_tie."""
    from github_statistics.render import plan_chart

    first, second, tie = case
    plan = plan_chart(_spec(first, second, back_on_tie=tie))

    assert len(plan.back) == len(plan.front) == len(first)
    for day, (back, front) in enumerate(zip(plan.back, plan.front, strict=True)):
        assert back.day == front.day == day
        assert back.value >= front.value
        assert {back.series, front.series} == {"opened", "closed"}
        values = {"opened": first[day], "closed": second[day]}
        assert back.value == values[back.series]
        assert front.value == values[front.series]
        if first[day] == second[day]:
            assert back.series == tie


def test_ticks_are_round_and_cover_the_peak() -> None:
    """Ticks start at zero, step by a round amount and reach the largest covered value."""
    from github_statistics.render import plan_chart

    plan = plan_chart(_spec([3, 47, 12], [9, 0, 130]))

    assert plan.ticks == (0, 25, 50, 75, 100, 125, 150)
    assert plan.y_max == 150


def test_thirty_day_render_draws_three_polygons_per_bar(tmp_path: Path) -> None:
    """Each non-zero bar on a covered day is three polygons; zero values draw none."""
    from github_statistics.render import render_chart

    first = [(i * 7) % 11 for i in range(30)]
    second = [(i * 5) % 9 for i in range(30)]
    nonzero = sum(v > 0 for v in first) + sum(v > 0 for v in second)

    chart = render_chart(
        _spec(first, second), tmp_path / "chart.png", style=_small_style()
    )

    assert chart.bar_polygons == 3 * nonzero
    assert chart.gap_slabs == 0
    assert chart.legend == ("Opened", "Closed")


def test_gap_days_draw_slabs_and_no_bars(tmp_path: Path) -> None:
    """Gap days draw a slab each run of days, no bars, and add their legend entries."""
    from github_statistics.render import Coverage, GapSpan, render_chart

    coverage = ["complete"] * 30
    coverage[4] = coverage[5] = "none"
    coverage[20] = "partial"
    values = [10] * 30

    chart = render_chart(
        _spec(values, values, coverage=coverage),
        tmp_path / "chart.png",
        style=_small_style(),
    )

    assert chart.plan.gaps == (
        GapSpan(4, 6, Coverage.NONE),
        GapSpan(20, 21, Coverage.PARTIAL),
    )
    assert chart.gap_slabs == 2
    assert chart.bar_polygons == 3 * 2 * 27
    assert chart.legend == ("Opened", "Closed", "no data", "partial data")


def test_legend_lists_only_the_gap_kinds_present() -> None:
    """A chart with only no-data days gains the "no data" entry alone."""
    from github_statistics.render import plan_chart

    coverage = ["complete", "none", "complete"]

    plan = plan_chart(_spec([1, 2, 3], [3, 2, 1], coverage=coverage))

    assert plan.legend == ("Opened", "Closed", "no data")


def test_two_renders_are_byte_identical(tmp_path: Path) -> None:
    """Rendering one spec twice writes the same bytes."""
    from github_statistics.render import render_chart

    spec = _spec(
        [1, 5, 3, 0, 8],
        [2, 2, 7, 1, 0],
        coverage=["complete", "none", "complete", "partial", "complete"],
    )

    a = render_chart(spec, tmp_path / "a.png", style=_small_style())
    b = render_chart(spec, tmp_path / "b.png", style=_small_style())

    assert a.path.read_bytes() == b.path.read_bytes()


def test_ambient_rc_settings_do_not_change_the_output(tmp_path: Path) -> None:
    """A caller's rcParams, such as a tight bbox, leave the PNG unchanged."""
    import matplotlib
    from PIL import Image

    from github_statistics.render import render_chart

    spec = _spec([3, 1, 4], [2, 7, 1])
    clean = render_chart(spec, tmp_path / "clean.png", style=_small_style())
    with matplotlib.rc_context(
        {"savefig.bbox": "tight", "savefig.transparent": True, "font.size": 30}
    ):
        ambient = render_chart(spec, tmp_path / "ambient.png", style=_small_style())

    with Image.open(ambient.path) as image:
        assert image.size == (ambient.width, ambient.height)
    assert ambient.path.read_bytes() == clean.path.read_bytes()


def test_render_writes_a_png_at_the_exact_path(tmp_path: Path) -> None:
    """The PNG lands at the path given, whatever its suffix."""
    from PIL import Image

    from github_statistics.render import render_chart

    spec = _spec([3, 1, 4], [2, 7, 1])
    for name in ("chart", "chart.jpg"):
        chart = render_chart(spec, tmp_path / name, style=_small_style())
        assert chart.path == tmp_path / name
        with Image.open(chart.path) as image:
            assert image.format == "PNG"
    assert not (tmp_path / "chart.png").exists()


def test_master_is_4k_and_derivatives_have_the_requested_width(tmp_path: Path) -> None:
    """The master is exactly 3840x2160; each derivative is named and sized by its width."""
    from PIL import Image

    from github_statistics.render import render_chart, write_derivatives

    chart = render_chart(
        _spec([3, 1, 4, 1, 5, 9, 2], [2, 7, 1, 8, 2, 8, 1]), tmp_path / "chart.png"
    )
    derivatives = write_derivatives(chart.path, [1920, 640])

    with Image.open(chart.path) as master:
        assert master.size == (3840, 2160)
    assert [p.name for p in derivatives] == ["chart-1920.png", "chart-640.png"]
    for path, width in zip(derivatives, (1920, 640), strict=True):
        with Image.open(path) as image:
            assert image.size == (width, width * 9 // 16)


def test_derivative_wider_than_master_is_rejected(tmp_path: Path) -> None:
    """Upscaling is refused."""
    from github_statistics.render import render_chart, write_derivatives

    chart = render_chart(_spec([1], [2]), tmp_path / "chart.png", style=_small_style())

    with pytest.raises(ValueError, match="outside"):
        write_derivatives(chart.path, [chart.width + 1])


@pytest.mark.parametrize(
    ("first", "second", "coverage", "polygons", "slabs"),
    [
        pytest.param([0] * 14, [0] * 14, None, 0, 0, id="all-zero"),
        pytest.param([4], [6], None, 6, 0, id="single-day"),
        pytest.param([3] * 730, [0] * 730, None, 3 * 730, 0, id="730-days"),
        pytest.param([5] * 10, [6] * 10, ["none"] * 10, 0, 1, id="all-gaps"),
    ],
)
def test_edge_cases_render(
    tmp_path: Path,
    first: list[int],
    second: list[int],
    coverage: list[str] | None,
    polygons: int,
    slabs: int,
) -> None:
    """Degenerate windows still render, with a positive scale."""
    from github_statistics.render import render_chart

    chart = render_chart(
        _spec(first, second, coverage=coverage),
        tmp_path / "chart.png",
        style=_small_style(),
    )

    assert chart.path.stat().st_size > 0
    assert chart.bar_polygons == polygons
    assert chart.gap_slabs == slabs
    assert chart.plan.y_max > 0


@pytest.mark.parametrize("peak_day", [0, 180, 322, 364])
def test_peak_labels_clear_the_legend(tmp_path: Path, peak_day: int) -> None:
    """Peak labels clear a four-entry legend and each other, even on the last days."""
    from github_statistics.render import render_chart

    first = [0] * 365
    first[peak_day] = 10
    second = [0] * 365
    second[364 - peak_day] = 4
    coverage = ["complete"] * 365
    coverage[100] = "none"
    coverage[101] = "partial"
    chart = render_chart(
        _spec(first, second, coverage=coverage),
        tmp_path / "chart.png",
        style=_small_style(),
    )

    assert len(chart.legend) == 4
    assert len(chart.peak_labels) == 2
    for label in chart.peak_labels:
        assert not label.box.overlaps(chart.legend_box)
    one, two = (label.box for label in chart.peak_labels)
    assert not one.overlaps(two)


@pytest.mark.parametrize("days", [30, 365])
def test_peak_labels_clear_taller_neighbours(tmp_path: Path, days: int) -> None:
    """A low peak among taller bars of the other series is labelled above them."""
    from github_statistics.render import render_chart
    from github_statistics.render.layout import Geometry

    peak_day = days // 2
    first = [0] * days
    first[peak_day] = 10
    second = [0] * days
    for day in range(max(peak_day - 10, 0), min(peak_day + 11, days)):
        if day != peak_day:
            second[day] = 100
    style = _small_style()
    chart = render_chart(_spec(first, second), tmp_path / "chart.png", style=style)

    geo = Geometry.build(style, days)
    scale = geo.scale(chart.plan.y_max)
    assert len(chart.peak_labels) == 2
    for label in chart.peak_labels:
        for row, bars in ((1, chart.plan.back), (0, chart.plan.front)):
            for bar in bars:
                left, base = geo.bar_origin(bar.day, row)
                if (
                    bar.value
                    and left < label.box.x1
                    and label.box.x0 < left + geo.width + geo.dx
                ):
                    assert label.box.y0 >= base + bar.value * scale + geo.dy


@pytest.mark.parametrize(("wide", "narrow"), [(50, 72), (72, 50)])
def test_peak_labels_of_unequal_width_stack(
    tmp_path: Path, wide: int, narrow: int
) -> None:
    """A narrow label near a wider one stacks clear of it, in either order."""
    from github_statistics.render import render_chart

    first = [0] * 365
    first[wide] = 100_000
    first[narrow] = 99_999
    second = [0] * 365
    second[narrow] = 1
    chart = render_chart(
        _spec(first, second), tmp_path / "chart.png", style=_small_style()
    )

    assert len(chart.peak_labels) == 2
    one, two = (label.box for label in chart.peak_labels)
    assert not one.overlaps(two)


@pytest.mark.parametrize("days", [7, 90, 365, 730])
def test_month_labels_do_not_collide(days: int) -> None:
    """Month labels thin out so neighbours stay at least a label apart."""
    from github_statistics.render import Style, plan_chart

    style = Style()
    plan = plan_chart(_spec([1] * days, [2] * days), style)
    pitch = (style.canvas_width - style.margin_left - style.margin_right) / days
    label_px = len("Mmm YYYY") * 0.62 * style.month_pt * style.reference_dpi / 72

    assert plan.month_labels
    centres = [label.centre * pitch for label in plan.month_labels]
    assert all(b - a >= label_px for a, b in zip(centres, centres[1:], strict=False))


def test_spec_rejects_days_that_are_not_consecutive() -> None:
    """A window with a missing day is an error, not a silently squashed chart."""
    from github_statistics.render import ChartSpec, Coverage, Series

    with pytest.raises(ValueError, match="consecutive"):
        ChartSpec(
            title="t",
            subtitle="s",
            footer="f",
            days=(START, START + timedelta(days=2)),
            first=Series("opened", "Opened", "#0072B2", (1, 2)),
            second=Series("closed", "Closed", "#E69F00", (2, 1)),
            coverage=(Coverage.COMPLETE, Coverage.COMPLETE),
            back_on_tie="closed",
        )
