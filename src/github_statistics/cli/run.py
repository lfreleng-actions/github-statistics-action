# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The ``run`` command: every stage for every target, as the action runs it (design §4.2, §9.1).

For each target in turn ``run`` clears the target's ``img/`` and
``data/`` directories, then collects, renders, exports and reads what
the target's pages show; the pages and the landing page are written
once, after every target. A spent budget is not a failure: rendering
and the site go on, and the charts show the gaps. Neither is the time
limit, which bounds collection alone: it counts from the command's
start, and the stages that need no network always run. A rejected token or
an invalid configuration stops the run before anything is rendered.
Exit codes are those of ``collect``: 1 for an invalid configuration or
a failed stage, 2 with ``--strict`` once the site is written if a
repository was unreadable, and 3 for a rejected token.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from github_statistics.config import ConfigError, Resolution, Target, default_cache_dir
from github_statistics.store import path_component, store_path

from .collect import (
    STRICT_EXIT,
    collect_outputs,
    collect_target,
    collection_deadline,
    report,
)
from .common import (
    API_HOST,
    ReadOnlyStore,
    StaleStoreError,
    append_summary,
    fail,
    write_outputs,
)
from .doctor import BOT_KEYS
from .export import DATA_DIR, export_targets
from .options import SharedOptions, shared_options
from .render import render_targets
from .site import site_targets, write_site

IMAGES_DIR = "img"


def clear_target_output(output_dir: Path, owner: str) -> None:
    """Remove ``owner``'s ``img/`` and ``data/`` under ``output_dir``, and nothing else.

    A file left by an earlier run, such as the export of a repository
    since purged, must not reach the published site. An owner directory
    that is a symbolic link is refused: clearing through it would
    delete, and the stages would then write, outside ``output_dir``.
    """
    owner_dir = output_dir / path_component(owner, "owner")
    if owner_dir.is_symlink():
        raise fail(f"refusing to clear {owner_dir}: it is a symbolic link")
    for name in (IMAGES_DIR, DATA_DIR):
        path = owner_dir / name
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def unclassified(resolution: Resolution, target: Target) -> list[str]:
    """The stored bots of ``target`` named in no identity list; none if unreadable."""
    from github_statistics.series import unclassified_bots

    config = resolution.config
    root = Path(config.cache.directory or default_cache_dir())
    try:
        with ReadOnlyStore(store_path(root, API_HOST, target.owner)) as store:
            return unclassified_bots(store, resolution.identities_for(target))
    except (StaleStoreError, sqlite3.DatabaseError):
        return []


def _echo(paths: Iterable[Path]) -> None:
    """Print each of ``paths``, one per line."""
    for path in paths:
        typer.echo(str(path))


@shared_options
def run(
    options: SharedOptions,
    refresh: bool = typer.Option(
        False, "--refresh", help="Ignore watermarks and coverage; re-read the window."
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Exit 2 if any repository was unreadable."
    ),
) -> None:
    """Collect, render, export and build the site for every target."""
    try:
        resolution = options.resolve()
    except ConfigError as exc:
        raise fail(str(exc)) from None
    config = resolution.config
    deadline = collection_deadline(config.budget.minutes)
    now = datetime.now(UTC)
    until = options.until or now.date() - timedelta(days=1)
    output_dir = Path(config.output.directory)
    results = []
    sites = []
    bots: set[str] = set()
    for target in config.targets:
        clear_target_output(output_dir, target.owner)
        results.append(
            collect_target(
                resolution,
                target,
                now=now,
                until=options.until,
                refresh=refresh,
                deadline=deadline,
            )
        )
        _echo(render_targets(resolution, [target], until=until, now=now))
        _echo(export_targets(resolution, [target], until=until, generated=now.date()))
        sites += site_targets(resolution, [target], until=until)
        bots.update(unclassified(resolution, target))
    _echo(write_site(sites, config, now=now))

    found = sorted(bots)
    summary = report(results)
    summary += f"### Site\n\nWritten to `{output_dir}`.\n\n"
    if found:
        summary += f"Unclassified bots: {', '.join(found)}; add each to {BOT_KEYS}.\n"
    append_summary(summary)
    write_outputs(
        {
            "site_dir": str(output_dir),
            **collect_outputs(results),
            "unclassified_bots": ",".join(found),
        },
        heredoc=True,
    )
    for login in found:
        typer.echo(f"warning: unclassified bot {login}: add it to {BOT_KEYS}", err=True)
    if strict and any(result.unreadable for result in results):
        raise fail("some repositories were unreadable (--strict)", STRICT_EXIT)
