# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The 2.5D chart renderer: plain data in, PNG files out, no network."""

from __future__ import annotations

from .bars3d import render_chart
from .images import write_derivatives
from .layout import plan_chart
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
    RenderedChart,
    Series,
    Style,
)

__all__ = [
    "Bar",
    "Box",
    "ChartPlan",
    "ChartSpec",
    "Coverage",
    "GapSpan",
    "MonthLabel",
    "Peak",
    "PeakLabel",
    "RenderedChart",
    "Series",
    "Style",
    "plan_chart",
    "render_chart",
    "write_derivatives",
]
