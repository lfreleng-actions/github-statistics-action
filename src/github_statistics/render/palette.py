# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Default colours for charts: Okabe-Ito series colours and chart furniture.

The series colours are colour-blind safe and stay distinct when two
series swap rows, which is how a chart shows that the smaller value of
a day changed hands.
"""

from __future__ import annotations

from types import MappingProxyType

SERIES_COLOURS = MappingProxyType(
    {
        "opened": "#0072B2",
        "closed": "#E69F00",
        "runs": "#009E73",
        "jobs": "#CC79A7",
        "succeeded": "#009E73",
        "failed": "#D55E00",
    }
)

BACKGROUND = "#F6F7F9"
FLOOR = "#E3E7ED"
WALL = "#ECEFF3"
GRIDLINE = "#FFFFFF"
SEPARATOR = "#C9D0DA"
TEXT = "#2B3140"
MUTED = "#6B7280"
NO_DATA = "#7C8594"
PARTIAL_DATA = "#A9B0BC"


def series_colour(key: str) -> str:
    """Return the default colour for the series named ``key``."""
    try:
        return SERIES_COLOURS[key]
    except KeyError:
        raise KeyError(f"no default colour for series {key!r}") from None
