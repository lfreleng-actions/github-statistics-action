# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Options every command that reads the configuration shares (design §4.2).

Decorate a command with `shared_options` and give it an ``options``
parameter: the decorator adds the shared flags to the command's
signature and passes them in as one `SharedOptions`.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, TypeVar

import typer

from github_statistics.config import Identities, Overrides, Resolution, resolve
from github_statistics.config.loader import split_list

F = TypeVar("F", bound=Callable[..., Any])

_LIST = "comma-, space- or newline-separated; repeatable"
_IDENTITY_HELP = f"replaces the configured list ({_LIST}; §7.2)"


@dataclass(frozen=True)
class SharedOptions:
    """The shared command line options, normalised.

    Empty strings count as not given, since the action passes an empty
    string for every input left unset.
    """

    config: str | None = None
    config_data: str | None = None
    until: date | None = None
    overrides: Overrides = field(default_factory=Overrides)

    def resolve(self) -> Resolution:
        """Resolve the configuration these options select."""
        return resolve(
            config_data=self.config_data,
            config_path=self.config,
            overrides=self.overrides,
        )


def _identity(values: list[str] | None) -> tuple[str, ...] | None:
    """An identity flag's list, or ``None`` when absent or empty."""
    return split_list(values) or None


def _build(**values: Any) -> SharedOptions:
    """Normalise the raw flag values and enforce the exclusivity rules."""
    include = split_list(values["include"])
    exclude = split_list(values["exclude"])
    repos = split_list(values["repo"])
    if include and exclude:
        raise typer.BadParameter(
            "cannot be combined with --exclude", param_hint="--include"
        )
    if repos and (include or exclude):
        raise typer.BadParameter(
            "cannot be combined with --include or --exclude", param_hint="--repo"
        )
    until = values["until"]
    overrides = Overrides(
        orgs=split_list(values["org"]),
        repos=repos,
        include=include,
        exclude=exclude,
        days=values["days"],
        output_dir=values["output_dir"] or None,
        cache_dir=values["cache_dir"] or None,
        budget=values["budget"],
        max_minutes=values["max_minutes"],
        token_env=values["token_env"] or None,
        identities=Identities(
            org_bots=_identity(values["org_bots"]),
            external_bots=_identity(values["external_bots"]),
            members=_identity(values["members"]),
            member_orgs=_identity(values["member_orgs"]),
            ignore_logins=_identity(values["ignore_logins"]),
        ),
    )
    return SharedOptions(
        config=values["config"] or None,
        config_data=values["config_data"] or None,
        until=until.date() if until is not None else None,
        overrides=overrides,
    )


def _option(
    name: str, annotation: Any, metavar: str, text: str, **extra: Any
) -> inspect.Parameter:
    """A keyword-only parameter carrying the Typer option ``--name``."""
    flag = "--" + name.replace("_", "-")
    info = typer.Option(None, flag, metavar=metavar, help=text, **extra)
    return inspect.Parameter(
        name, inspect.Parameter.KEYWORD_ONLY, default=info, annotation=annotation
    )


def _parameters() -> list[inspect.Parameter]:
    """The shared options as parameters Typer reads from a signature."""
    text, lists, number = str | None, list[str] | None, int | None
    return [
        _option("config", text, "PATH", "Configuration file."),
        _option(
            "config_data", text, "JSON", "Configuration as JSON or base64 of JSON."
        ),
        _option("org", lists, "OWNER", f"Owner to report on ({_LIST})."),
        _option("repo", lists, "OWNER/NAME", f"Repository to report on ({_LIST})."),
        _option(
            "include", lists, "NAMES", f"Repository names or globs to keep ({_LIST})."
        ),
        _option(
            "exclude", lists, "NAMES", f"Repository names or globs to drop ({_LIST})."
        ),
        _option("days", number, "N", "Window length in days."),
        _option(
            "until",
            datetime | None,
            "YYYY-MM-DD",
            "Last day of the window (default: yesterday, UTC).",
            formats=["%Y-%m-%d"],
        ),
        _option("output_dir", text, "DIR", "Site root."),
        _option("cache_dir", text, "DIR", "Store root."),
        _option("budget", number, "N", "REST requests this run may spend."),
        _option(
            "max_minutes",
            number,
            "N",
            "Minutes collection may take (default: no limit).",
        ),
        _option("token_env", text, "NAME", "Environment variable holding the token."),
        _option(
            "org_bots", lists, "LOGINS", f"The organisation's bots; {_IDENTITY_HELP}."
        ),
        _option("external_bots", lists, "LOGINS", f"Outside bots; {_IDENTITY_HELP}."),
        _option(
            "members", lists, "LOGINS", f"Humans counted as members; {_IDENTITY_HELP}."
        ),
        _option(
            "member_orgs",
            lists,
            "OWNERS",
            f"Organisations whose public members count; {_IDENTITY_HELP}.",
        ),
        _option(
            "ignore_logins", lists, "LOGINS", f"Accounts left out; {_IDENTITY_HELP}."
        ),
    ]


def shared_options(command: F) -> F:
    """Add the shared options to ``command``, passed in as ``options: SharedOptions``."""
    extra = _parameters()
    names = [parameter.name for parameter in extra]
    own = [
        parameter
        for parameter in inspect.signature(command, eval_str=True).parameters.values()
        if parameter.name != "options"
    ]

    def wrapper(**kwargs: Any) -> Any:
        shared = {name: kwargs.pop(name) for name in names}
        return command(options=_build(**shared), **kwargs)

    wrapper.__name__ = command.__name__
    wrapper.__qualname__ = command.__qualname__
    wrapper.__doc__ = command.__doc__
    wrapper.__module__ = command.__module__
    parameters = own + extra
    wrapper.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    wrapper.__annotations__ = {p.name: p.annotation for p in parameters}
    return wrapper  # type: ignore[return-value]
