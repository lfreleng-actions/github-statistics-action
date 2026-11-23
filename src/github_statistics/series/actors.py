# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Actor classes, assigned when the series are built, never stored (design §7.1).

Changing a bot list and re-rendering so corrects every chart with no
new requests.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from enum import StrEnum

from ..config import ResolvedIdentities
from ..store import Store

BOT_SUFFIX = "[bot]"
BOT_TYPE = "Bot"


class ActorClass(StrEnum):
    """The four classes every counted actor falls into."""

    MEMBER = "member"
    EXTERNAL = "external"
    ORG_BOT = "org_bot"
    EXTERNAL_BOT = "external_bot"


CLASSES: tuple[ActorClass, ...] = tuple(ActorClass)
AGGREGATES: dict[str, tuple[ActorClass, ...]] = {
    "humans": (ActorClass.MEMBER, ActorClass.EXTERNAL),
    "bots": (ActorClass.ORG_BOT, ActorClass.EXTERNAL_BOT),
    "all": CLASSES,
}


def normalise(login: str) -> str:
    """``login`` lower-cased, without a trailing ``[bot]``.

    REST reports ``dependabot[bot]`` where GraphQL reports ``dependabot``;
    both forms name the same actor.
    """
    folded = login.strip().lower()
    if folded.endswith(BOT_SUFFIX):
        folded = folded[: -len(BOT_SUFFIX)]
    return folded


def _normalised(logins: Iterable[str]) -> frozenset[str]:
    """Each of ``logins``, normalised."""
    return frozenset(normalise(login) for login in logins)


def classify(
    login: str | None,
    account_type: str | None,
    identities: ResolvedIdentities,
    public_members: Collection[str],
) -> ActorClass | None:
    """The class of one actor, or ``None`` when its login is ignored.

    The first matching rule wins: ``ignore_logins``, ``org_bots``,
    ``external_bots``, an unlisted ``Bot`` (external, and unclassified),
    the explicit ``members``, ``public_members``, then external. An
    actor GitHub reports without a login (a deleted account) is
    external. ``public_members`` holds normalised logins, as
    `public_members` returns them.
    """
    if login is None:
        return ActorClass.EXTERNAL
    key = normalise(login)
    if key in _normalised(identities.ignore_logins.values):
        return None
    if key in _normalised(identities.org_bots.values):
        return ActorClass.ORG_BOT
    if key in _normalised(identities.external_bots.values):
        return ActorClass.EXTERNAL_BOT
    if account_type == BOT_TYPE:
        return ActorClass.EXTERNAL_BOT
    if key in _normalised(identities.members.values) or key in public_members:
        return ActorClass.MEMBER
    return ActorClass.EXTERNAL


def is_unclassified(
    login: str, account_type: str | None, identities: ResolvedIdentities
) -> bool:
    """Whether `classify` places this actor by its unlisted-``Bot`` rule."""
    if account_type != BOT_TYPE:
        return False
    listed = (
        identities.ignore_logins.values
        + identities.org_bots.values
        + identities.external_bots.values
    )
    return normalise(login) not in _normalised(listed)


def public_members(
    store: Store, identities: ResolvedIdentities, *, owner: str
) -> frozenset[str]:
    """The normalised public members of every organisation in ``member_orgs``.

    ``owner``, the target's owner, and each name in ``member_orgs`` count
    as members too: an organisation never acts on its own, and for a
    target owned by a user the owner, who has no member list, is a
    member whatever ``member_orgs`` holds (design §7.1). Organisation
    names match whatever their case.
    """
    orgs = identities.member_orgs.values
    stored = store.member_logins(orgs)
    return _normalised((owner, *orgs)) | _normalised(stored)


def unclassified_bots(store: Store, identities: ResolvedIdentities) -> list[str]:
    """The logins of stored ``Bot`` actors named in no identity list, sorted.

    These are the actors `classify` places as external bots by its
    fallback rule, for the end-of-run warning and ``doctor``. A login
    stored in both its REST and GraphQL forms is listed once.
    """
    found: dict[str, str] = {}
    for login, account_type in store.actors():
        if is_unclassified(login, account_type, identities):
            key = normalise(login)
            found[key] = min(found.get(key, login), login)
    return sorted(found.values())
