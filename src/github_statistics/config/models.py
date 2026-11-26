# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Frozen dataclasses mirroring the configuration schema.

Lists become tuples so a loaded configuration cannot change under the
code that reads it. The defaults here are the defaults of the
configuration document (design Appendix A).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

Logins = tuple[str, ...]


@dataclass(frozen=True)
class Identities:
    """Identity lists set at one level; ``None`` means the level leaves a key alone."""

    org_bots: Logins | None = None
    external_bots: Logins | None = None
    members: Logins | None = None
    member_orgs: Logins | None = None
    ignore_logins: Logins | None = None


@dataclass(frozen=True)
class Target:
    """One owner and the rule that selects its repositories."""

    owner: str
    token_env: str = "GITHUB_TOKEN"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    scan_archived: bool = False
    scan_forks: bool = False
    scan_templates: bool = True
    identities: Identities = field(default_factory=Identities)


@dataclass(frozen=True)
class Window:
    """The length of the reporting window in days."""

    days: int = 365


@dataclass(frozen=True)
class Metrics:
    """Which metrics a run collects and charts."""

    pull_requests: bool = True
    issues: bool = True
    workflows: bool = True
    workflow_outcomes: bool = False


@dataclass(frozen=True)
class BackOnTie:
    """Per metric, the series drawn in the back row when both values are equal."""

    pull_requests: str = "closed"
    issues: str = "closed"
    workflows: str = "jobs"


@dataclass(frozen=True)
class Render:
    """Chart size, derivatives and colours."""

    width: int = 3840
    height: int = 2160
    derivatives: tuple[int, ...] = (1920,)
    svg: bool = False
    palette: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    back_on_tie: BackOnTie = field(default_factory=BackOnTie)


@dataclass(frozen=True)
class Contributors:
    """The contributor table: how many rows and which extra columns."""

    top_n: int = 100
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Budget:
    """API spending limits for one run.

    ``reserve`` stays ``None`` unless configured, so the transport can
    choose its default: 500 for a token, 10 for anonymous reads.
    ``minutes`` bounds how long collection may take; ``None`` sets no
    time limit.
    """

    rest: int = 3000
    graphql_points: int = 1500
    reserve: int | None = None
    minutes: int | None = None


@dataclass(frozen=True)
class Output:
    """Where the site is written."""

    directory: str = "site"


@dataclass(frozen=True)
class Cache:
    """Where the store lives; ``None`` means the XDG cache directory."""

    directory: str | None = None


DEFAULT_CLASSES = ("all", "member", "external", "org_bot", "external_bot")


@dataclass(frozen=True)
class Config:
    """A whole, validated configuration document."""

    targets: tuple[Target, ...]
    version: int = 1
    identities: Identities = field(default_factory=Identities)
    window: Window = field(default_factory=Window)
    metrics: Metrics = field(default_factory=Metrics)
    classes: tuple[str, ...] = DEFAULT_CLASSES
    render: Render = field(default_factory=Render)
    contributors: Contributors = field(default_factory=Contributors)
    budget: Budget = field(default_factory=Budget)
    output: Output = field(default_factory=Output)
    cache: Cache = field(default_factory=Cache)


@dataclass(frozen=True)
class ResolvedList:
    """One resolved identity list and the level it came from.

    ``source`` is ``"cli"``, ``"target"``, ``"config"`` or ``"default"``.
    """

    values: Logins
    source: str


@dataclass(frozen=True)
class ResolvedIdentities:
    """The identity lists in force for one target, each with its provenance."""

    org_bots: ResolvedList
    external_bots: ResolvedList
    members: ResolvedList
    member_orgs: ResolvedList
    ignore_logins: ResolvedList
