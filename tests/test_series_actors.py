# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for actor classification (design §7.1)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

MEMORY = Path(":memory:")


def _identities(**lists: Any) -> Any:
    """The identity lists in force for ``acme``, or ``owner``, with ``lists`` configured."""
    from github_statistics.config import Config, Identities, Target, resolve_identities

    target = Target(lists.pop("owner", "acme"))
    return resolve_identities(
        target, Config(targets=(target,), identities=Identities(**lists))
    )


def _classify(
    login: str | None,
    account_type: str | None = "User",
    public: frozenset[str] = frozenset(),
    **lists: tuple[str, ...],
) -> str | None:
    """The class ``classify`` gives ``login``, as its string value."""
    from github_statistics.series import classify

    result = classify(login, account_type, _identities(**lists), public)
    return None if result is None else str(result)


@pytest.mark.xfail(strict=True)
def test_ignored_login_wins_over_every_list() -> None:
    assert (
        _classify(
            "bot", "Bot", ignore_logins=("bot",), org_bots=("bot",), members=("bot",)
        )
        is None
    )
    assert (
        _classify("bot", "User", ignore_logins=("BOT[bot]",), members=("bot",)) is None
    )


@pytest.mark.xfail(strict=True)
def test_org_bots_win_over_external_bots() -> None:
    assert (
        _classify("helper", "Bot", org_bots=("helper[bot]",), external_bots=("helper",))
        == "org_bot"
    )


@pytest.mark.xfail(strict=True)
def test_listed_external_bot() -> None:
    assert (
        _classify("dependabot[bot]", "Bot", external_bots=("dependabot[bot]",))
        == "external_bot"
    )


@pytest.mark.xfail(strict=True)
def test_listed_bot_of_type_user_follows_its_list() -> None:
    assert _classify("Copilot", "User", external_bots=("Copilot",)) == "external_bot"
    assert _classify("releaser", "User", org_bots=("releaser",)) == "org_bot"


@pytest.mark.xfail(strict=True)
def test_unlisted_bot_is_external_even_when_named_a_member() -> None:
    assert _classify("newapp[bot]", "Bot") == "external_bot"
    assert _classify("newapp", "Bot", members=("newapp",)) == "external_bot"


@pytest.mark.xfail(strict=True)
@pytest.mark.parametrize(
    ("login", "listed"),
    [
        ("dependabot[bot]", "dependabot"),
        ("dependabot", "dependabot[bot]"),
        ("Dependabot[BOT]", "dependabot"),
        ("DEPENDABOT", "Dependabot[bot]"),
    ],
)
def test_bot_suffix_and_case_are_ignored(login: str, listed: str) -> None:
    assert _classify(login, "Bot", org_bots=(listed,)) == "org_bot"


@pytest.mark.xfail(strict=True)
def test_explicit_members_list() -> None:
    assert _classify("Alice") == "external"
    assert _classify("Alice", "User", members=("alice",)) == "member"


@pytest.mark.xfail(strict=True)
def test_public_member_is_member_and_private_member_external() -> None:
    from github_statistics.series import classify

    identities = _identities()
    public = frozenset({"alice"})
    assert classify("ALICE", "User", identities, public) == "member"
    assert classify("bob", "User", identities, public) == "external"


@pytest.mark.xfail(strict=True)
def test_missing_login_is_external() -> None:
    assert _classify(None, None) == "external"


@pytest.mark.xfail(strict=True)
def test_public_members_read_every_member_org_from_the_store() -> None:
    from github_statistics.series.actors import public_members
    from github_statistics.store import Store

    with Store(MEMORY) as store:
        store.replace_members("acme", ["Alice"], date(2026, 3, 1))
        store.replace_members("friends", ["bob"], date(2026, 3, 1))
        store.replace_members("others", ["carol"], date(2026, 3, 1))
        identities = _identities(member_orgs=("acme", "friends"))
        members = public_members(store, identities, owner="acme")
    assert {"alice", "bob"} <= members
    assert "carol" not in members


@pytest.mark.xfail(strict=True)
def test_public_members_match_member_orgs_whatever_their_case() -> None:
    from github_statistics.series import classify
    from github_statistics.series.actors import public_members
    from github_statistics.store import Store

    identities = _identities(member_orgs=("ACME",))
    with Store(MEMORY) as store:
        store.replace_members("Acme", ["alice"], date(2026, 3, 1))
        members = public_members(store, identities, owner="acme")
    assert classify("alice", "User", identities, members) == "member"
    assert classify("bob", "User", identities, members) == "external"


@pytest.mark.xfail(strict=True)
def test_owner_of_a_user_owned_target_is_a_member() -> None:
    from github_statistics.series import classify
    from github_statistics.series.actors import public_members
    from github_statistics.store import Store

    identities = _identities(owner="Jane")
    with Store(MEMORY) as store:
        members = public_members(store, identities, owner="Jane")
    assert classify("jane", "User", identities, members) == "member"
    assert classify("joe", "User", identities, members) == "external"


@pytest.mark.xfail(strict=True)
def test_owner_is_a_member_whatever_member_orgs_holds() -> None:
    from github_statistics.series import classify
    from github_statistics.series.actors import public_members
    from github_statistics.store import Store

    identities = _identities(owner="Jane", member_orgs=("acme",))
    ignoring = _identities(owner="Jane", member_orgs=("acme",), ignore_logins=("jane",))
    with Store(MEMORY) as store:
        store.replace_members("acme", ["alice"], date(2026, 3, 1))
        members = public_members(store, identities, owner="Jane")
    assert classify("Jane", "User", identities, members) == "member"
    assert classify("alice", "User", identities, members) == "member"
    assert classify("joe", "User", identities, members) == "external"
    assert classify("jane", "User", ignoring, members) is None


@pytest.mark.xfail(strict=True)
def test_unclassified_bots_lists_unlisted_bot_actors_once() -> None:
    from github_statistics.series import unclassified_bots
    from github_statistics.store import Item, Run, Store

    at = datetime(2026, 3, 2, 12, tzinfo=UTC)
    item: dict[str, Any] = {
        "node_id": "I",
        "kind": "pr",
        "created_at": at,
        "closed_at": None,
        "merged_at": None,
        "updated_at": at,
    }
    run: dict[str, Any] = {
        "node_id": "R",
        "repo": "acme/a",
        "workflow_path": "ci.yaml",
        "status": "completed",
        "conclusion": "success",
        "created_at": at,
        "attempt": 1,
    }
    with Store(MEMORY) as store:
        store.upsert_items(
            [
                Item("acme/a", 1, author="newapp[bot]", author_type="Bot", **item),
                Item("acme/a", 2, author="newapp", author_type="Bot", **item),
                Item("acme/a", 3, author="dependabot[bot]", author_type="Bot", **item),
                Item("acme/a", 4, author="alice", author_type="User", **item),
            ]
        )
        store.upsert_runs(
            [
                Run(1, event="push", actor="ci-app[bot]", actor_type="Bot", **run),
                Run(2, event="schedule", actor="cron[bot]", actor_type="Bot", **run),
            ]
        )
        found = unclassified_bots(store, _identities(external_bots=("dependabot",)))
    assert found == ["ci-app[bot]", "newapp"]
