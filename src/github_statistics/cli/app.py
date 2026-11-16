# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The Typer application behind the ``github-statistics`` command."""

from __future__ import annotations

import typer

from github_statistics import __version__

app = typer.Typer(
    name="github-statistics",
    help="Daily, visual activity statistics for GitHub organisations and repositories.",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    """Print the package version and exit when ``--version`` is given."""
    if value:
        typer.echo(f"github-statistics {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Daily, visual activity statistics for GitHub organisations and repositories."""
