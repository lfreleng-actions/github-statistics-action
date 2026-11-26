# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The ``site`` command: the static site from the store (design §4.2, §8.5).

Needs no token and sends no request. Each store opens read-only, as for
``export``; a missing or stale store fails naming ``collect``, and a
repository selection narrower than the store fails as it does for
``render``. Every store is read before the first page is written. The
pages link to the charts ``render`` wrote and the files ``export``
wrote; this command writes neither. Standard output lists the files
written, one per line.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from github_statistics.config import (
    Config,
    ConfigError,
    Resolution,
    ResolvedIdentities,
    Target,
    default_cache_dir,
)
from github_statistics.store import Store, store_path

from .common import API_HOST, ReadOnlyStore, StaleStoreError, fail
from .options import SharedOptions, shared_options
from .render import _unselected

if TYPE_CHECKING:
    from github_statistics.site import SiteTarget


def _merged(
    store: Store,
    owner: str,
    days: tuple[date, ...],
    identities: ResolvedIdentities,
) -> int:
    """Pull requests merged over ``days``, classed as the daily series classes them.

    A deleted author counts, as it does in the series; an ignored login
    does not.
    """
    from github_statistics.series.actors import classify, public_members

    members = public_members(store, identities, owner=owner)
    return sum(
        count
        for login, account_type, count in store.item_actor_counts(
            "pr", "merged_at", days[0], days[-1]
        )
        if classify(login, account_type, identities, members) is not None
    )


def site_targets(
    resolution: Resolution, targets: Sequence[Target], *, until: date
) -> list[SiteTarget]:
    """Read what the pages of ``targets`` show from their stores, up to ``until``.

    Every store is read before any page is written; a missing, stale or
    unreadable store, or a selection narrower than the store, raises the
    exit `fail` builds.
    """
    # Imported here so other commands start without loading matplotlib.
    from github_statistics.series import (
        build_leaderboard,
        build_repositories,
        build_series,
    )
    from github_statistics.site import SiteTarget

    config = resolution.config
    root = Path(config.cache.directory or default_cache_dir())
    stores = {
        target.owner: store_path(root, API_HOST, target.owner) for target in targets
    }
    for owner, path in stores.items():
        if not path.is_file():
            raise fail(f"no store for {owner} at {path}: run `collect` first")
    built = []
    for target in targets:
        path = stores[target.owner]
        identities = resolution.identities_for(target)
        try:
            with ReadOnlyStore(path) as store:
                series = build_series(
                    store, config, target, until=until, identities=identities
                )
                contributors = build_leaderboard(
                    store, config, target, until=until, identities=identities
                )
                repositories = build_repositories(
                    store, config, target, until=until, identities=identities
                )
                merged = _merged(store, target.owner, series.days, identities)
        except sqlite3.DatabaseError as exc:
            raise fail(f"cannot read the store at {path}: {exc}") from None
        except StaleStoreError:
            raise fail(f"the store at {path} is stale: run `collect` first") from None
        dropped = _unselected(target, [row.repo for row in repositories])
        if dropped:
            raise fail(
                f"the store for {target.owner} holds {', '.join(dropped)} in the"
                " window, outside the repository selection, and site cannot"
                " leave it out: build without the selection, or `collect` the"
                " selection into its own --cache-dir"
            )
        built.append(
            SiteTarget(target, series, contributors, repositories, merged, identities)
        )
    return built


def write_site(
    sites: Sequence[SiteTarget], config: Config, *, now: datetime
) -> list[Path]:
    """Write the pages of ``sites`` and the landing page; the files written."""
    from github_statistics.site import build_site

    return build_site(sites, config, Path(config.output.directory), now=now)


@shared_options
def site(options: SharedOptions) -> None:
    """Write the HTML pages of every target from the store; needs no token.

    The output directory must hold the charts and data of the same run,
    from `render` and `export`; site links them and does not check their
    freshness.
    """
    try:
        resolution = options.resolve()
    except ConfigError as exc:
        raise fail(str(exc)) from None
    config = resolution.config
    now = datetime.now(UTC)
    until = options.until or now.date() - timedelta(days=1)
    built = site_targets(resolution, config.targets, until=until)
    for written in write_site(built, config, now=now):
        typer.echo(str(written))
