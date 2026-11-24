# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""CSV files, one per table, from the export documents (design §8.6).

``series.csv`` is in long form, one row per metric, series, class and
day; ``coverage.csv`` one row per metric and day; the contributor,
repository and review tables one row per record. The runs horizon and
the run metadata have no table and stay in the JSON export.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

Table = tuple[list[str], list[list[Any]]]


def _series(document: Mapping[str, Any]) -> Table:
    """The long-form rows of ``series.json``."""
    rows: list[list[Any]] = []
    for metric, data in sorted(document["metrics"].items()):
        for series, classes in sorted(data["series"].items()):
            for actor_class, points in sorted(classes.items()):
                rows.extend(
                    [metric, series, actor_class, point["day"], point["value"]]
                    for point in points
                )
    return ["metric", "series", "class", "day", "value"], rows


def _coverage(document: Mapping[str, Any]) -> Table:
    """One row per metric and day, from the per-day coverage in ``series.json``."""
    rows = [
        [metric, point["day"], point["coverage"]]
        for metric, data in sorted(document["metrics"].items())
        for point in data["coverage"]
    ]
    return ["metric", "day", "coverage"], rows


def _contributors(document: Mapping[str, Any]) -> Table:
    """The leaderboard, one row per contributor."""
    header = ["login", "class", *document["columns"]]
    return header, [[row[key] for key in header] for row in document["contributors"]]


def _repositories(document: Mapping[str, Any]) -> Table:
    """The repository table, with one ``coverage_<metric>`` column per metric."""
    metrics = document["metrics"]
    rows = document["repositories"]
    counts = document["columns"]
    header = ["repository", *counts, *(f"coverage_{metric}" for metric in metrics)]
    body = [
        [
            row["repository"],
            *(row[key] for key in counts),
            *(row["coverage"][metric] for metric in metrics),
        ]
        for row in rows
    ]
    return header, body


def _reviews(document: Mapping[str, Any]) -> Table:
    """One row per review."""
    header = ["repository", "number", "reviewer", "class", "state", "submitted_at"]
    return header, [[row[key] for key in header] for row in document["reviews"]]


def tables(documents: Mapping[str, Any]) -> Iterator[tuple[str, Table]]:
    """Each CSV table, by file stem, built from the export documents."""
    yield "series", _series(documents["series"])
    yield "coverage", _coverage(documents["series"])
    yield "contributors", _contributors(documents["contributors"])
    yield "repositories", _repositories(documents["repositories"])
    yield "reviews", _reviews(documents["reviews"])


def write_csv(documents: Mapping[str, Any], directory: Path) -> list[Path]:
    """Write each table to ``<directory>/<name>.csv``; the paths, sorted."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, (header, rows) in tables(documents):
        path = directory / f"{name}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)
        paths.append(path)
    return sorted(paths)
