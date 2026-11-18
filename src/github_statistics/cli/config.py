# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The ``config`` command group: show, check and path."""

from __future__ import annotations

import json
from typing import Any

import typer

from github_statistics.config import ConfigError, Resolution, default_config_path
from github_statistics.config.loader import dump, looks_like_token

from .options import SharedOptions, shared_options

REDACTED = "<redacted: looks like a token>"

config_app = typer.Typer(
    name="config",
    help="Show, check or locate the configuration.",
    no_args_is_help=True,
)


def _resolve(options: SharedOptions) -> Resolution:
    """Resolve the configuration, or exit 1 with a one-line reason."""
    try:
        return options.resolve()
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from None


def _redact(value: Any) -> Any:
    """``value`` with every token-shaped string, at any depth, redacted."""
    if isinstance(value, str):
        return REDACTED if looks_like_token(value) else value
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _report(resolution: Resolution) -> dict[str, Any]:
    """The resolved configuration and identity provenance, tokens redacted."""
    identities = []
    for target in resolution.config.targets:
        resolved = dump(resolution.identities_for(target))
        identities.append({"owner": target.owner, **resolved})
    report = {
        "source": resolution.source,
        "location": resolution.location,
        "config": dump(resolution.config),
        "identities": identities,
    }
    return _redact(report)


@config_app.command()
@shared_options
def show(options: SharedOptions) -> None:
    """Print the resolved configuration and where each identity list came from."""
    typer.echo(json.dumps(_report(_resolve(options)), indent=2))


@config_app.command()
@shared_options
def check(options: SharedOptions) -> None:
    """Validate the configuration; exit non-zero with a one-line reason on failure."""
    resolution = _resolve(options)
    where = f" ({resolution.location})" if resolution.location else ""
    typer.echo(f"ok: configuration from {resolution.source}{where}")


@config_app.command()
def path() -> None:
    """Print the default configuration file path."""
    typer.echo(str(default_config_path()))
