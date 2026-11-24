# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The contributor leaderboard, credited to authors (design §7.4).

One row per actor over the window, bots included. Pull requests and
issues count for whoever authored them, so ``prs_merged`` is the
author's; the reviewer columns count the reviews each actor submitted.
The two forms of a bot login, ``name`` and ``name[bot]``, share a row.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

from ..config import Config, ResolvedIdentities, Target, resolve_identities
from ..store import Store
from .actors import BOT_SUFFIX, ActorClass, classify, normalise, public_members
from .daily import window

AUTHORED = (
    "prs_opened",
    "prs_merged",
    "prs_closed_unmerged",
    "issues_opened",
    "issues_closed",
)
PRS_REVIEWED = "prs_reviewed"
REVIEWED = ("reviews", PRS_REVIEWED, "approvals", "changes_requested")
# The review columns `AggregateStore.review_actor_counts` returns, in order.
_REVIEW_COUNTS = ("reviews", "approvals", "changes_requested")
BOT_TYPE = "Bot"
RUNS_TRIGGERED = "runs_triggered"
TOTAL = "total"
# The columns ``total`` sums: every default count but ``total`` itself.
SUMMED = (*AUTHORED, *REVIEWED, RUNS_TRIGGERED)
COLUMNS = (*SUMMED, TOTAL)
# The optional columns, by the ``contributors.columns`` name that enables each.
OPTIONAL = {"merged_by": "merged_as_merger", "closed_by": "closed_as_closer"}
# When the two forms of a login classify apart, the first class listed wins.
_PRECEDENCE = (
    ActorClass.ORG_BOT,
    ActorClass.EXTERNAL_BOT,
    ActorClass.MEMBER,
    ActorClass.EXTERNAL,
)

# The authored columns: item kind, timestamp column and whether only unmerged count.
_AUTHORED_QUERIES = {
    "prs_opened": ("pr", "created_at", False),
    "prs_merged": ("pr", "merged_at", False),
    "prs_closed_unmerged": ("pr", "closed_at", True),
    "issues_opened": ("issue", "created_at", False),
    "issues_closed": ("issue", "closed_at", False),
}
_OPTIONAL_QUERIES = {
    "merged_as_merger": ("pr", "merged_at", "merged_by"),
    "closed_as_closer": (None, "closed_at", "closed_by"),
}


@dataclass(frozen=True)
class Contributor:
    """One leaderboard row: the display login, its public class and its counts."""

    login: str
    actor_class: ActorClass
    counts: Mapping[str, int]

    def record(self) -> dict[str, str | int]:
        """The row as one flat record: ``login``, ``class`` and each count."""
        return {"login": self.login, "class": self.actor_class.value, **self.counts}


def display_login(
    forms: Iterable[str], identities: ResolvedIdentities, *, bot: bool = False
) -> str:
    """The display form of one actor seen under ``forms``.

    REST gives a bot's login with its ``[bot]`` suffix where GraphQL
    drops it, so a suffixed form wins, whether stored or named in a bot
    list; otherwise the first stored form in sort order, given the
    suffix when ``bot`` says GitHub typed the account ``Bot``.
    """
    stored = sorted(set(forms))
    key = normalise(stored[0])
    listed = [
        login
        for login in (*identities.org_bots.values, *identities.external_bots.values)
        if normalise(login) == key
    ]
    suffixed = [f for f in (*stored, *listed) if f.lower().endswith(BOT_SUFFIX)]
    if suffixed:
        return suffixed[0]
    return f"{stored[0]}{BOT_SUFFIX}" if bot else stored[0]


class _Board:
    """Counts per normalised login, with every form, class and type seen for each."""

    def __init__(self, identities: ResolvedIdentities, members: frozenset[str]) -> None:
        """Start empty; classes resolve once per ``(login, type)``."""
        self._identities = identities
        self._members = members
        self._classes: dict[tuple[str, str | None], ActorClass | None] = {}
        self.forms: dict[str, set[str]] = {}
        self.seen: dict[str, set[ActorClass]] = {}
        self.bots: set[str] = set()
        self.counts: dict[str, dict[str, int]] = {}
        self.distinct: dict[str, dict[str, set[object]]] = {}

    def _key(self, login: str | None, account_type: str | None) -> str | None:
        """The row key of one actor, recording its form; ``None`` when left out."""
        if login is None:
            return None
        pair = (login, account_type)
        if pair not in self._classes:
            self._classes[pair] = classify(
                login, account_type, self._identities, self._members
            )
        actor_class = self._classes[pair]
        if actor_class is None:
            return None
        key = normalise(login)
        self.forms.setdefault(key, set()).add(login)
        self.seen.setdefault(key, set()).add(actor_class)
        if account_type == BOT_TYPE:
            self.bots.add(key)
        self.counts.setdefault(key, {})
        return key

    def add(
        self, login: str | None, account_type: str | None, column: str, count: int
    ) -> None:
        """Credit ``count`` in ``column`` to one actor, unless ignored or unnamed."""
        if not count:
            return
        key = self._key(login, account_type)
        if key is not None:
            row = self.counts[key]
            row[column] = row.get(column, 0) + count

    def add_distinct(
        self, login: str | None, account_type: str | None, column: str, item: object
    ) -> None:
        """Credit ``item`` in ``column`` to one actor, once whatever its form."""
        key = self._key(login, account_type)
        if key is not None:
            self.distinct.setdefault(key, {}).setdefault(column, set()).add(item)

    def rows(self, columns: tuple[str, ...]) -> list[Contributor]:
        """Every row, each count column filled and ``total`` summed."""
        rows = []
        for key, row in self.counts.items():
            counted = dict(row)
            for column, items in self.distinct.get(key, {}).items():
                counted[column] = len(items)
            values = {column: counted.get(column, 0) for column in columns}
            values[TOTAL] = sum(counted.get(column, 0) for column in SUMMED)
            actor_class = next(c for c in _PRECEDENCE if c in self.seen[key])
            login = display_login(
                self.forms[key], self._identities, bot=key in self.bots
            )
            rows.append(Contributor(login, actor_class, MappingProxyType(values)))
        return rows


def leaderboard_columns(config: Config) -> tuple[str, ...]:
    """The count columns of the table: the defaults, then any optional ones enabled."""
    extra = tuple(
        OPTIONAL[name] for name in OPTIONAL if name in config.contributors.columns
    )
    return (*COLUMNS, *extra)


def build_leaderboard(
    store: Store,
    config: Config,
    target: Target,
    *,
    until: date,
    identities: ResolvedIdentities | None = None,
) -> list[Contributor]:
    """The contributor rows of ``target`` over the window ending on ``until``.

    Sorted by ``total`` descending, then login; ``contributors.top_n``
    caps the rows, ``0`` keeping them all. Ignored logins, scheduled
    runs and events from repositories out of scope that day are left out.
    """
    if identities is None:
        identities = resolve_identities(target, config)
    days = window(config.window.days, until)
    first, last = days[0], days[-1]
    board = _Board(identities, public_members(store, identities, owner=target.owner))
    for column, (kind, field, unmerged) in _AUTHORED_QUERIES.items():
        for login, account_type, count in store.item_actor_counts(
            kind, field, first, last, unmerged=unmerged
        ):
            board.add(login, account_type, column, count)
    for login, account_type, *counts in store.review_actor_counts(first, last):
        for column, count in zip(_REVIEW_COUNTS, counts, strict=True):
            board.add(login, account_type, column, count)
    for login, account_type, repo, number in store.reviewed_pull_requests(first, last):
        board.add_distinct(login, account_type, PRS_REVIEWED, (repo, number))
    for login, account_type, count in store.run_actor_counts(first, last):
        board.add(login, account_type, RUNS_TRIGGERED, count)
    columns = leaderboard_columns(config)
    for column, (optional_kind, field, actor) in _OPTIONAL_QUERIES.items():
        if column in columns:
            for login, account_type, count in store.item_actor_counts(
                optional_kind, field, first, last, actor=actor
            ):
                board.add(login, account_type, column, count)
    rows = sorted(
        board.rows(columns),
        key=lambda row: (-row.counts[TOTAL], row.login.lower(), row.login),
    )
    top_n = config.contributors.top_n
    return rows[:top_n] if top_n else rows
