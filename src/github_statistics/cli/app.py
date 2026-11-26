# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The Typer application behind the ``github-statistics`` command."""

from __future__ import annotations

import logging
import sys
import time
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import typer

from github_statistics import __version__

from .cache import cache_app
from .collect import collect_command
from .config import config_app
from .doctor import doctor
from .export import export
from .render import render
from .run import run
from .scope import scope
from .site import site

DERIVATIVE_WIDTH = 1920
DEMO_MIN_DAYS = 7
DEMO_MAX_DAYS = 730
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOG_HANDLER = "github-statistics"
# httpx logs every request URL at INFO; these speak only at DEBUG.
QUIET_LOGGERS = ("httpx", "httpcore", "matplotlib")

app = typer.Typer(
    name="github-statistics",
    help="Daily, visual activity statistics for GitHub organisations and repositories.",
    no_args_is_help=True,
)
app.add_typer(config_app)
app.add_typer(cache_app)
app.command()(doctor)
app.command()(scope)
app.command()(export)
app.command(name="collect")(collect_command)
app.command()(render)
app.command()(site)
app.command()(run)


def _version_callback(value: bool) -> None:
    """Print the package version and exit when ``--version`` is given."""
    if value:
        typer.echo(f"github-statistics {__version__}")
        raise typer.Exit()


class LogLevel(StrEnum):
    """The levels ``--log-level`` accepts."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


def configure_logging(level: LogLevel) -> None:
    """Log to stderr at ``level``, with UTC timestamps.

    A handler is added only when the root logger has none. Third-party
    loggers stay at WARNING unless ``level`` is DEBUG; the handler takes
    ``level`` too, since their records skip the root logger's own level.
    """
    numeric = logging.getLevelName(level.value.upper())
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(LOG_HANDLER)
    formatter = logging.Formatter(LOG_FORMAT)
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logging.basicConfig(level=numeric, handlers=[handler])
    root = logging.getLogger()
    root.setLevel(numeric)
    for installed in root.handlers:
        if installed.get_name() == LOG_HANDLER:
            installed.setLevel(numeric)
    quiet = logging.DEBUG if level is LogLevel.DEBUG else logging.WARNING
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(quiet)


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
    log_level: LogLevel = typer.Option(
        LogLevel.INFO,
        "--log-level",
        envvar="GHS_LOG_LEVEL",
        case_sensitive=False,
        help="Level of the progress log written to stderr.",
    ),
) -> None:
    """Daily, visual activity statistics for GitHub organisations and repositories."""
    configure_logging(log_level)


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
