# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Turn a target into the public repositories to read (design §5).

The resolver is where the privacy rule for repositories holds: every
repository whose ``visibility`` is not ``public``, listed or named, is
dropped with the reason ``not public``, and no option relaxes this.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import quote

import httpx

from github_statistics.client import Transport, TransportError, get_paged
from github_statistics.config import Target

log = logging.getLogger(__name__)

NOT_PUBLIC = "not public"
ARCHIVED = "archived"
FORK = "fork"
TEMPLATE = "template"
EXCLUDED = "excluded"
NOT_INCLUDED = "not included"
NOT_FOUND = "not found"

_GLOB_CHARACTERS = frozenset("*?[")


@dataclass(frozen=True)
class Repository:
    """One public repository in scope."""

    name: str
    created_at: str
    archived: bool
    fork: bool
    is_template: bool
    default_branch: str


@dataclass(frozen=True)
class Dropped:
    """A repository left out of scope, and why."""

    name: str
    reason: str


@dataclass(frozen=True)
class ResolvedScope:
    """The repositories one target resolves to.

    ``repositories`` and ``dropped`` are sorted by name. ``unresolved``
    names the repositories whose read failed, so their fate is unknown.
    """

    owner: str
    owner_type: str
    repositories: tuple[Repository, ...]
    dropped: tuple[Dropped, ...]
    unresolved: tuple[str, ...] = ()


def is_exact(entry: str) -> bool:
    """Whether ``entry`` names one repository rather than a glob."""
    return not _GLOB_CHARACTERS.intersection(entry)


def _path(*parts: str) -> str:
    """An API path from ``parts``, each escaped as one path segment."""
    return "/" + "/".join(quote(part, safe="") for part in parts)


def _matches(name: str, patterns: Iterable[str]) -> bool:
    """Whether ``name`` matches any of ``patterns``, ignoring case."""
    return any(fnmatchcase(name.lower(), pattern.lower()) for pattern in patterns)


def _is_public(data: dict[str, Any]) -> bool:
    """Whether GitHub reports ``data`` as public, by both fields."""
    return data.get("visibility") == "public" and not data.get("private", False)


def _listing_reason(data: dict[str, Any], target: Target) -> str | None:
    """Why a listed repository is dropped, or ``None`` when it is kept."""
    name = data["name"]
    if not _is_public(data):
        return NOT_PUBLIC
    if data.get("archived") and not target.scan_archived:
        return ARCHIVED
    if data.get("fork") and not target.scan_forks:
        return FORK
    if data.get("is_template") and not target.scan_templates:
        return TEMPLATE
    if target.include and not _matches(name, target.include):
        return NOT_INCLUDED
    if _matches(name, target.exclude):
        return EXCLUDED
    return None


def _repository(data: dict[str, Any]) -> Repository:
    """The fields of a repository the store and collectors use."""
    return Repository(
        name=data["name"],
        created_at=data["created_at"],
        archived=bool(data.get("archived")),
        fork=bool(data.get("fork")),
        is_template=bool(data.get("is_template")),
        default_branch=data.get("default_branch") or "",
    )


def _known_names(owner: str, known: Iterable[str]) -> list[str]:
    """Bare names from ``known``, which may hold ``owner/name`` store keys.

    A key under another owner is not this target's to resolve, so it is
    left out.
    """
    names: list[str] = []
    for entry in known:
        prefix, slash, name = entry.rpartition("/")
        if not slash:
            names.append(entry)
        elif prefix.lower() == owner.lower():
            names.append(name)
    return names


async def _lookup(transport: Transport, owner: str, name: str) -> dict[str, Any] | None:
    """``GET /repos/{owner}/{name}``; ``None`` on a ``404``, a rename or a transfer."""
    response = await transport.request("GET", _path("repos", owner, name))
    if response.status_code == 404:
        return None
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    # A renamed or transferred repository redirects to a different one.
    full_name = str(data.get("full_name", ""))
    return data if full_name.lower() == f"{owner}/{name}".lower() else None


async def _listing(
    transport: Transport, owner: str, owner_type: str
) -> list[dict[str, Any]]:
    """Every repository the owner's listing returns, all pages."""
    if owner_type == "Organization":
        url, kind = _path("orgs", owner, "repos"), "public"
    else:
        url, kind = _path("users", owner, "repos"), "owner"
    listed: list[dict[str, Any]] = []
    async for items, _ in get_paged(transport, url, {"type": kind, "per_page": 100}):
        listed.extend(items)
    return listed


async def resolve_scope(
    transport: Transport, target: Target, *, known: Iterable[str] = ()
) -> ResolvedScope:
    """Resolve ``target`` to its public repositories (design §5).

    A non-empty ``include`` of exact names reads each name alone and
    applies no ``scan_*`` toggle; anything else lists the owner's
    repositories and filters them. ``known`` names repositories stored
    by earlier runs, as bare names or ``owner/name`` store keys: each one the listing no longer returns is read
    alone, so one that turned private is reported ``not public``, and
    one GitHub no longer shows, ``not found``. A failed read of a single
    repository leaves it in ``unresolved``. ``BudgetExhaustedError``
    and ``AuthError`` propagate, as does a failed owner or listing read.
    """
    response = await transport.request("GET", _path("users", target.owner))
    response.raise_for_status()
    user = response.json()
    owner, owner_type = user["login"], user["type"]

    explicit = bool(target.include) and all(map(is_exact, target.include))
    found: dict[str, dict[str, Any]] = {}
    if not explicit:
        for data in await _listing(transport, owner, owner_type):
            found[data["name"].lower()] = data

    drops: dict[str, Dropped] = {}
    unresolved: list[str] = []
    wanted = [*target.include] if explicit else []
    for name in dict.fromkeys([*wanted, *_known_names(owner, known)]):
        key = name.lower()
        if key in found or key in drops or name in unresolved:
            continue
        try:
            looked_up = await _lookup(transport, owner, name)
        except (httpx.HTTPStatusError, TransportError) as exc:
            log.warning("could not read %s/%s: %s", owner, name, exc)
            unresolved.append(name)
            continue
        if looked_up is None:
            log.warning("skipping %s/%s: not found", owner, name)
            drops[key] = Dropped(name, NOT_FOUND)
        else:
            found[key] = looked_up

    named = {name.lower() for name in wanted}
    kept: list[Repository] = []
    for key, data in found.items():
        if key in named:
            reason = None if _is_public(data) else NOT_PUBLIC
        else:
            reason = _listing_reason(data, target)
        if reason == NOT_PUBLIC:
            log.warning("skipping %s/%s: not public", owner, data["name"])
        if reason is None:
            kept.append(_repository(data))
        else:
            drops[key] = Dropped(data["name"], reason)

    return ResolvedScope(
        owner=owner,
        owner_type=owner_type,
        repositories=tuple(sorted(kept, key=lambda repo: repo.name.lower())),
        dropped=tuple(sorted(drops.values(), key=lambda drop: drop.name.lower())),
        unresolved=tuple(sorted(unresolved, key=str.lower)),
    )
