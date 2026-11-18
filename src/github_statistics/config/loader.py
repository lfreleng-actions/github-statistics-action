# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Find, read, validate and resolve the configuration (design §7.2, Appendix A).

One loader reads every source: a file, a raw JSON value, or base64 of
JSON, so a plain ``vars.`` value and a base64 ``secrets.`` value both
work. `resolve` is the single entry point the command line uses; it
picks the source, applies command line overrides and reports which
source won. Tokens never come from the configuration, only from the
environment variable a target names (`resolve_token`).
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .models import (
    BackOnTie,
    Budget,
    Cache,
    Config,
    Contributors,
    Identities,
    Metrics,
    Output,
    Render,
    ResolvedIdentities,
    ResolvedList,
    Target,
    Window,
)
from .schema import IDENTITY_KEYS, ConfigError, NoConfigurationError, validate

logger = logging.getLogger(__name__)

APP_NAME = "github-statistics"
CONFIG_ENV = "GHS_CONFIG"
TOKEN_PREFIXES = ("ghp_", "github_pat_", "ghs_", "gho_", "ghu_", "ghr_")

_SEPARATORS = re.compile(r"[,\s]+")
_REPO_NAME = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Overrides:
    """Command line values that override the configuration.

    Empty tuples and ``None`` mean "not given". `identities` holds the
    identity flags; they outrank every configuration level (§7.2).
    """

    orgs: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    days: int | None = None
    output_dir: str | None = None
    cache_dir: str | None = None
    budget: int | None = None
    token_env: str | None = None
    identities: Identities = field(default_factory=Identities)


@dataclass(frozen=True)
class Resolution:
    """The configuration in force, where it came from, and the identity flags.

    ``source`` is ``"config-data"``, ``"config"``, ``"env"``,
    ``"default"`` or ``"flags"``; ``location`` names the file or
    variable it was read from, where there is one.
    """

    config: Config
    source: str
    location: str | None = None
    cli_identities: Identities = field(default_factory=Identities)

    def identities_for(self, target: Target) -> ResolvedIdentities:
        """Resolve the identity lists for ``target``, flags included."""
        return resolve_identities(target, self.config, self.cli_identities)


def looks_like_token(value: str) -> bool:
    """Whether ``value`` has the shape of a GitHub token rather than a variable name.

    Surrounding whitespace is ignored, since a pasted token often carries
    some; ``value`` itself is left as configured.
    """
    return value.strip().startswith(TOKEN_PREFIXES)


def split_list(values: Iterable[str] | None) -> tuple[str, ...]:
    """Split flag or input values on commas, spaces and newlines.

    A flag may repeat, so ``values`` is every occurrence; empty items
    are dropped, so ``--org-bots ""`` yields an empty tuple.
    """
    items: list[str] = []
    for value in values or ():
        items.extend(item for item in _SEPARATORS.split(value) if item)
    return tuple(items)


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    """The environment to read: ``env`` when given, else the process environment."""
    return os.environ if env is None else env


def _xdg_dir(env: Mapping[str, str], variable: str, fallback: str) -> Path:
    """An XDG base directory for this tool, falling back under the home directory."""
    base = env.get(variable)
    if base:
        return Path(base) / APP_NAME
    home = Path(env["HOME"]) if env.get("HOME") else Path.home()
    return home / fallback / APP_NAME


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_CONFIG_HOME/github-statistics/config.json``, else under ``~/.config``."""
    return _xdg_dir(_env(env), "XDG_CONFIG_HOME", ".config") / "config.json"


def default_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_CACHE_HOME/github-statistics``, else ``~/.cache/github-statistics``."""
    return _xdg_dir(_env(env), "XDG_CACHE_HOME", ".cache")


def _parse(raw: str) -> dict[str, Any]:
    """Decode raw JSON, or base64 of JSON, into a document (not yet validated)."""
    text = raw.strip()
    if not text:
        raise ConfigError("configuration is empty")
    if not text.startswith(("{", "[")):
        try:
            text = base64.b64decode("".join(text.split()), validate=True).decode()
        except (binascii.Error, ValueError):
            raise ConfigError(
                "configuration is neither JSON nor base64-encoded JSON"
            ) from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"configuration is not valid JSON: {exc.msg} "
            f"at line {exc.lineno} column {exc.colno}"
        ) from None
    validate(data)
    return data


def _logins(data: Mapping[str, Any] | None) -> Identities:
    """Build an `Identities`, keeping absent keys as ``None``."""
    data = data or {}
    return Identities(**{key: tuple(data[key]) for key in IDENTITY_KEYS if key in data})


def _target(data: Mapping[str, Any]) -> Target:
    """Build a `Target` from its validated document."""
    values = dict(data)
    for key in ("include", "exclude"):
        if key in values:
            values[key] = tuple(values[key])
    values["identities"] = _logins(data.get("identities"))
    return Target(**values)


def _render(data: Mapping[str, Any]) -> Render:
    """Build `Render` from its validated document."""
    values = dict(data)
    if "derivatives" in values:
        values["derivatives"] = tuple(values["derivatives"])
    values["palette"] = MappingProxyType(dict(data.get("palette", {})))
    values["back_on_tie"] = BackOnTie(**data.get("back_on_tie", {}))
    return Render(**values)


def _build(data: Mapping[str, Any]) -> Config:
    """Build a `Config` from a validated document."""
    contributors = dict(data.get("contributors", {}))
    if "columns" in contributors:
        contributors["columns"] = tuple(contributors["columns"])
    optional: dict[str, Any] = {}
    if "version" in data:
        optional["version"] = data["version"]
    if "classes" in data:
        optional["classes"] = tuple(data["classes"])
    return Config(
        targets=tuple(_target(target) for target in data["targets"]),
        identities=_logins(data.get("identities")),
        window=Window(**data.get("window", {})),
        metrics=Metrics(**data.get("metrics", {})),
        render=_render(data.get("render", {})),
        contributors=Contributors(**contributors),
        budget=Budget(**data.get("budget", {})),
        output=Output(**data.get("output", {})),
        cache=Cache(**data.get("cache", {})),
        **optional,
    )


def _finish(data: Mapping[str, Any]) -> Config:
    """Validate a document, build the models and warn about token-shaped names."""
    validate(data)
    config = _build(data)
    for index, target in enumerate(config.targets):
        if looks_like_token(target.token_env):
            logger.warning(
                "targets[%d].token_env looks like a token value; it must name "
                "the environment variable that holds the token",
                index,
            )
    return config


def loads(raw: str) -> Config:
    """Load a configuration from raw JSON or base64 of JSON; raise `ConfigError`."""
    return _finish(_parse(raw))


def _read(path: Path) -> str:
    """Read a configuration file, turning I/O failures into `ConfigError`."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        raise ConfigError(f"cannot read {path}: {reason}") from None


def load_file(path: Path | str) -> Config:
    """Load a configuration file; raise `ConfigError` naming the file on failure."""
    path = Path(path)
    return _finish(_labelled(_read(path), str(path)))


def _split_repo(value: str) -> tuple[str, str]:
    """Split ``OWNER/NAME``; raise `ConfigError` on any other shape.

    The name becomes an ``include`` entry, which accepts globs, so it is
    held to the characters GitHub allows in a repository name: ``--repo``
    selects exact repositories, never a pattern.
    """
    owner, _, name = value.partition("/")
    if not owner or not _REPO_NAME.fullmatch(name):
        raise ConfigError(f"--repo {value!r}: expected OWNER/NAME")
    return owner, name


def _select_targets(
    configured: list[dict[str, Any]], overrides: Overrides
) -> list[dict[str, Any]]:
    """The targets ``--org`` and ``--repo`` select, built on configured ones."""
    by_owner = {target["owner"].lower(): target for target in configured}
    selected: dict[str, dict[str, Any]] = {}
    for org in overrides.orgs:
        selected.setdefault(
            org.lower(), dict(by_owner.get(org.lower(), {"owner": org}))
        )
    repos: dict[str, tuple[str, list[str]]] = {}
    for value in overrides.repos:
        owner, name = _split_repo(value)
        if owner.lower() in selected:
            raise ConfigError(f"--repo {value!r} overlaps --org {owner!r}")
        repos.setdefault(owner.lower(), (owner, []))[1].append(name)
    for key, (owner, names) in repos.items():
        target = dict(by_owner.get(key, {"owner": owner}))
        target["include"] = list(dict.fromkeys(names))
        target["exclude"] = []
        selected[key] = target
    return list(selected.values())


def _check_overrides(overrides: Overrides) -> None:
    """Reject the override combinations the command line refuses at parse time.

    Applying them would erase the conflict before validation sees it:
    ``include`` would silently drop ``exclude``, and either would widen
    or narrow the exact repositories ``repos`` names.
    """
    if overrides.include and overrides.exclude:
        raise ConfigError("--include cannot be combined with --exclude")
    if overrides.repos and (overrides.include or overrides.exclude):
        raise ConfigError("--repo cannot be combined with --include or --exclude")


def _apply(data: Mapping[str, Any], overrides: Overrides) -> dict[str, Any]:
    """Return a copy of ``data`` with the command line overrides applied."""
    result = copy.deepcopy(dict(data))
    if overrides.orgs or overrides.repos:
        result["targets"] = _select_targets(result.get("targets", []), overrides)
    for target in result.get("targets", []):
        if overrides.include:
            target["include"], target["exclude"] = list(overrides.include), []
        elif overrides.exclude:
            target["include"], target["exclude"] = [], list(overrides.exclude)
        if overrides.token_env:
            target["token_env"] = overrides.token_env
    sections = (
        ("window", "days", overrides.days),
        ("output", "directory", overrides.output_dir),
        ("cache", "directory", overrides.cache_dir),
        ("budget", "rest", overrides.budget),
    )
    for section, key, value in sections:
        if value is not None:
            result[section] = {**result.get(section, {}), key: value}
    return result


def _source(
    config_data: str | None,
    config_path: Path | str | None,
    overrides: Overrides,
    env: Mapping[str, str],
) -> tuple[dict[str, Any], str, str | None]:
    """Pick the highest-precedence source and parse it."""
    if config_data:
        return _labelled(config_data, "--config-data"), "config-data", None
    if config_path:
        path = Path(config_path)
        return _labelled(_read(path), str(path)), "config", str(path)
    if env.get(CONFIG_ENV):
        return _labelled(env[CONFIG_ENV], f"${CONFIG_ENV}"), "env", f"${CONFIG_ENV}"
    path = default_config_path(env)
    if path.is_file():
        return _labelled(_read(path), str(path)), "default", str(path)
    if overrides.orgs or overrides.repos:
        return {"targets": []}, "flags", None
    raise NoConfigurationError(
        f"no configuration: pass --config or --config-data, set ${CONFIG_ENV}, "
        f"create {path}, or name --org or --repo"
    )


def _labelled(raw: str, label: str) -> dict[str, Any]:
    """Parse ``raw``, prefixing any error with the source it came from."""
    try:
        return _parse(raw)
    except ConfigError as exc:
        raise ConfigError(f"{label}: {exc}") from None


def resolve(
    *,
    config_data: str | None = None,
    config_path: Path | str | None = None,
    overrides: Overrides | None = None,
    env: Mapping[str, str] | None = None,
) -> Resolution:
    """Find the configuration, apply the overrides and report the winning source.

    Source precedence, highest first: ``config_data`` (``--config-data``);
    ``config_path`` (``--config``); ``$GHS_CONFIG``, holding JSON or
    base64 of JSON; the default path; and with none of those,
    ``--org``/``--repo`` overrides alone build targets from defaults.
    Overrides that combine ``include`` with ``exclude``, or ``repos``
    with either, raise `ConfigError`; finding no source at all raises
    its subclass `NoConfigurationError`.
    """
    overrides = overrides or Overrides()
    _check_overrides(overrides)
    data, source, location = _source(config_data, config_path, overrides, _env(env))
    config = _finish(_apply(data, overrides))
    return Resolution(config, source, location, overrides.identities)


def resolve_identities(
    target: Target, config: Config, cli: Identities | None = None
) -> ResolvedIdentities:
    """Resolve each identity list for ``target`` (design §7.2).

    Per key, the highest level that sets it wins and replaces the lists
    below: the command line, the target's ``identities``, the top-level
    ``identities``, then the default. Lists default to empty;
    ``member_orgs`` defaults to, and when empty means, the target owner.
    """
    levels = (
        ("cli", cli or Identities()),
        ("target", target.identities),
        ("config", config.identities),
    )
    resolved: dict[str, ResolvedList] = {}
    for key in IDENTITY_KEYS:
        values: tuple[str, ...] = ()
        source = "default"
        for name, identities in levels:
            value = getattr(identities, key)
            if value is not None:
                values, source = value, name
                break
        if key == "member_orgs" and not values:
            values = (target.owner,)
        resolved[key] = ResolvedList(tuple(values), source)
    return ResolvedIdentities(**resolved)


def resolve_token(target: Target, env: Mapping[str, str] | None = None) -> str | None:
    """The token for ``target`` from the variable it names, or ``None`` if unset."""
    return _env(env).get(target.token_env) or None


def dump(value: Any) -> Any:
    """Convert a configuration model to plain JSON data.

    `Identities` keys a level leaves alone are omitted, so the result
    loads back to an equal model.
    """
    if isinstance(value, Identities):
        return {
            key: list(getattr(value, key))
            for key in IDENTITY_KEYS
            if getattr(value, key) is not None
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: dump(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {key: dump(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [dump(item) for item in value]
    return value
