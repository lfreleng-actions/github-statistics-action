# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Downscaled copies of a rendered master for inline use."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PIL import Image


def write_derivatives(master: Path, widths: Sequence[int]) -> list[Path]:
    """Write a Lanczos downscale of ``master`` per width, as ``<stem>-<width>.png``.

    Each copy keeps the master's aspect ratio. A width that is not
    positive or exceeds the master's raises :class:`ValueError`.
    """
    written: list[Path] = []
    with Image.open(master) as image:
        source = image.convert("RGB")
    for width in widths:
        if not 0 < width <= source.width:
            raise ValueError(f"width {width} is outside 1..{source.width}")
        height = max(1, round(source.height * width / source.width))
        target = master.with_name(f"{master.stem}-{width}.png")
        source.resize((width, height), Image.Resampling.LANCZOS).save(
            target, optimize=True
        )
        written.append(target)
    return written
