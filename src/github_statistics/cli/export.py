# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The ``export`` command: the dataset as JSON or CSV (design §4.2, §8.6).

It reads each target's store and nothing else: it needs no token,
makes no request, and never creates or rebuilds a store. Files go to
``<output>/<owner>/data/``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import typer

from github_statistics import __version__
from github_statistics.config import ConfigError, Resolution, Target, default_cache_dir
from github_statistics.store import path_component, store_path

from .common import API_HOST, ReadOnlyStore, StaleStoreError, fail
from .options import SharedOptions, shared_options

DATA_DIR = "data"


class Format(StrEnum):
    """Which files ``export`` writes."""

    JSON = "json"
    CSV = "csv"


def export_targets(
    resolution: Resolution,
    targets: Sequence[Target],
    *,
    until: date,
    generated: date,
    output_format: Format = Format.JSON,
) -> list[Path]:
    """Write each of ``targets``' documents under ``<output>/<owner>/data/``.

    ``until`` ends the window and ``generated`` dates the documents. The
    files written come back in order; a missing, stale or unreadable
    store raises the exit `fail` builds.
    """
    # Imported here so other commands start without loading matplotlib.
    from github_statistics.export import build_documents, write_csv, write_json

    config = resolution.config
    cache = (
        Path(config.cache.directory) if config.cache.directory else default_cache_dir()
    )
    write = write_csv if output_format is Format.CSV else write_json
    written: list[Path] = []
    for target in targets:
        path = store_path(cache, API_HOST, target.owner)
        if not path.is_file():
            raise fail(f"no store for {target.owner} at {path}; run collect first")
        try:
            store = ReadOnlyStore(path)
        except sqlite3.DatabaseError as exc:
            raise fail(f"cannot read the store at {path}: {exc}") from None
        except StaleStoreError:
            raise fail(f"the store at {path} is stale; run collect first") from None
        with store:
            documents = build_documents(
                store,
                config,
                target,
                until=until,
                generated=generated,
                version=__version__,
                identities=resolution.identities_for(target),
            )
        directory = (
            Path(config.output.directory)
            / path_component(target.owner, "owner")
            / DATA_DIR
        )
        written += write(documents, directory)
    return written


@shared_options
def export(
    options: SharedOptions,
    output_format: Format = typer.Option(
        Format.JSON, "--format", help="Write JSON documents or CSV tables."
    ),
) -> None:
    """Write each target's series, tables and coverage from the store; no network."""
    try:
        resolution = options.resolve()
    except ConfigError as exc:
        raise fail(str(exc)) from None
    now = datetime.now(UTC).date()
    until = options.until or now - timedelta(days=1)
    for target in resolution.config.targets:
        for written in export_targets(
            resolution,
            [target],
            until=until,
            generated=now,
            output_format=output_format,
        ):
            typer.echo(str(written))
