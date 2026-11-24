# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The Typer application behind the ``github-statistics`` command."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import typer

from github_statistics import __version__

from .cache import cache_app
from .collect import collect_command
from .config import config_app
from .export import export
from .render import render
from .scope import scope

DERIVATIVE_WIDTH = 1920
DEMO_MIN_DAYS = 7
DEMO_MAX_DAYS = 730

app = typer.Typer(
    name="github-statistics",
    help="Daily, visual activity statistics for GitHub organisations and repositories.",
    no_args_is_help=True,
)
app.add_typer(config_app)
app.add_typer(cache_app)
app.command()(scope)
app.command()(export)
app.command(name="collect")(collect_command)
app.command()(render)


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


def _yesterday() -> date:
    """The last complete UTC day, the default end of a window."""
    return datetime.now(UTC).date() - timedelta(days=1)


@app.command()
def demo(
    output_dir: Path = typer.Option(
        ..., "--output-dir", help="Directory to write the charts to."
    ),
    days: int = typer.Option(
        365,
        "--days",
        min=DEMO_MIN_DAYS,
        max=DEMO_MAX_DAYS,
        help="Window length in days.",
    ),
    seed: int = typer.Option(7, "--seed", help="Seed for the synthetic data."),
    until: datetime | None = typer.Option(
        None,
        "--until",
        formats=["%Y-%m-%d"],
        help="Last day of the window (default: yesterday, UTC).",
    ),
) -> None:
    """Render sample charts from synthetic data; needs no token or configuration."""
    # Imported here so other commands start without loading matplotlib.
    from github_statistics.render import render_chart, write_derivatives
    from github_statistics.render.demo import demo_specs

    end = until.date() if until is not None else _yesterday()
    for name, spec in demo_specs(days, seed, end):
        started = time.perf_counter()
        chart = render_chart(spec, output_dir / f"{name}.png")
        derivatives = write_derivatives(chart.path, [DERIVATIVE_WIDTH])
        elapsed = time.perf_counter() - started
        for path in (chart.path, *derivatives):
            typer.echo(f"{path}  {path.stat().st_size / 1024:.0f} KiB")
        typer.echo(f"  rendered {name} in {elapsed:.2f} s")
