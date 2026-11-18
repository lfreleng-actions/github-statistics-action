# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Where a store lives under the cache directory."""

from __future__ import annotations

import re
from pathlib import Path

STORE_FILE = "stats.sqlite"

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def path_component(text: str, what: str = "name") -> str:
    """``text`` reduced to a safe, lower-case path component; ``what`` names it in errors."""
    safe = _UNSAFE.sub("_", text.lower()).lstrip(".-")
    if not safe:
        raise ValueError(f"{what} {text!r} leaves no usable path component")
    return safe


def store_path(cache_dir: Path, api_host: str, owner: str) -> Path:
    """``<cache_dir>/<api_host>/<owner>/stats.sqlite``, both components sanitised.

    Each component keeps only ``[A-Za-z0-9_.-]``, lower-cased, with
    leading dots and dashes stripped, so no owner name can climb out of
    the cache directory; owners compare case-insensitively on GitHub, so
    ``Acme`` and ``acme`` share a store.
    """
    return (
        Path(cache_dir)
        / path_component(api_host, "API host")
        / path_component(owner, "owner")
        / STORE_FILE
    )
