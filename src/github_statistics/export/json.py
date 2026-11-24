# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Compact JSON files, one self-contained document each (design §8.6).

No indent, no spaces after separators, sorted keys and UTF-8 text, so a
fixed store gives byte-identical files and a large ``reviews.json``
stays a fraction of its pretty-printed size.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def compact(document: Any) -> str:
    """``document`` as compact JSON text with sorted keys."""
    return json.dumps(
        document, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    )


def write_json(documents: Mapping[str, Any], directory: Path) -> list[Path]:
    """Write each document to ``<directory>/<name>.json``; the paths, sorted."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in sorted(documents):
        path = directory / f"{name}.json"
        path.write_text(compact(documents[name]), encoding="utf-8")
        paths.append(path)
    return paths
