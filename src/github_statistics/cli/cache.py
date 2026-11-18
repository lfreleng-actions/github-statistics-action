# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The ``cache`` command group: info and clear (design §4.2)."""

from __future__ import annotations

from pathlib import Path

import typer

from github_statistics.config import (
    ConfigError,
    NoConfigurationError,
    default_cache_dir,
    resolve,
)
from github_statistics.store import (
    SCHEMA_VERSION,
    STORE_FILE,
    describe,
    path_component,
)

# SQLite's side files, removed along with the store they belong to.
_SUFFIXES = ("", "-journal", "-wal", "-shm")

cache_app = typer.Typer(
    name="cache",
    help="Inspect or clear the local store.",
    no_args_is_help=True,
)

_CACHE_DIR = typer.Option(
    None,
    "--cache-dir",
    metavar="DIR",
    help=(
        "Store root (default: the configured cache.directory, else"
        " $XDG_CACHE_HOME/github-statistics)."
    ),
)
_CONFIG = typer.Option(None, "--config", metavar="PATH", help="Configuration file.")
_CONFIG_DATA = typer.Option(
    None,
    "--config-data",
    metavar="JSON",
    help="Configuration as JSON or base64 of JSON.",
)


def _root(cache_dir: str | None, config: str | None, config_data: str | None) -> Path:
    """The store root: ``--cache-dir``, the configured one, or the XDG default.

    With no configuration source at all the XDG default applies; a
    configuration that cannot be read or validated exits 1 rather than
    falling back, so ``clear`` never empties a directory nobody chose.
    An explicit empty ``--cache-dir``, ``--config`` or ``--config-data``,
    such as an unset variable substituted into it, exits 1 for the same
    reason.
    """
    if cache_dir is not None:
        if not cache_dir:
            typer.echo("error: --cache-dir must not be empty", err=True)
            raise typer.Exit(1)
        return Path(cache_dir)
    for flag, value in (("--config", config), ("--config-data", config_data)):
        if value is not None and not value:
            typer.echo(f"error: {flag} must not be empty", err=True)
            raise typer.Exit(1)
    try:
        directory = resolve(
            config_data=config_data, config_path=config
        ).config.cache.directory
    except NoConfigurationError:
        directory = None
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from None
    return Path(directory) if directory else default_cache_dir()


def _stores(root: Path, owner: str = "*") -> list[Path]:
    """Every store file under ``root``, optionally for one sanitised owner.

    ``glob`` follows symbolic links, so a candidate whose host
    directory, owner directory or store file is a link is skipped
    before anything resolves it, a cyclic link included, and so is one
    that is not a regular file: ``clear`` never deletes or removes
    anything outside the cache directory, nor a link inside it.
    """
    return sorted(
        path
        for path in root.glob(f"*/{owner}/{STORE_FILE}")
        if not any(
            level.is_symlink() for level in (path.parent.parent, path.parent, path)
        )
        and path.is_file()
    )


@cache_app.command()
def info(
    cache_dir: str | None = _CACHE_DIR,
    config: str | None = _CONFIG,
    config_data: str | None = _CONFIG_DATA,
) -> None:
    """Print each store's path, size, schema version and row counts."""
    root = _root(cache_dir, config, config_data)
    typer.echo(f"cache: {root}")
    stores = _stores(root)
    if not stores:
        typer.echo("no stores")
        return
    for path in stores:
        found = describe(path)
        stale = "" if found.schema_version == SCHEMA_VERSION else " (stale)"
        typer.echo(str(found.path))
        typer.echo(f"  size: {found.size} bytes")
        typer.echo(f"  schema version: {found.schema_version}{stale}")
        for table, count in found.counts.items():
            typer.echo(f"  {table}: {count}")


@cache_app.command()
def clear(
    cache_dir: str | None = _CACHE_DIR,
    config: str | None = _CONFIG,
    config_data: str | None = _CONFIG_DATA,
    owner: str | None = typer.Option(
        None, "--owner", metavar="OWNER", help="Clear this owner's store alone."
    ),
) -> None:
    """Delete the stores under the cache directory, or one owner's store.

    Only store files and their regular side files are removed, never
    a link or anything else under the directory; the next ``collect``
    rebuilds them from GitHub.
    """
    root = _root(cache_dir, config, config_data)
    try:
        pattern = "*" if owner is None else path_component(owner, "owner")
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from None
    stores = _stores(root, pattern)
    if not stores:
        typer.echo("nothing to clear")
        return
    for path in stores:
        for suffix in _SUFFIXES:
            side = path.with_name(path.name + suffix)
            if not side.is_symlink() and side.is_file():
                side.unlink()
        for directory in (path.parent, path.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                break
        typer.echo(f"removed {path}")
