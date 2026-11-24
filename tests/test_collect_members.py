# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the public member collector (design §7.1)."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx

API = "https://api.github.com"
TODAY = date(2026, 3, 10)

pytestmark = pytest.mark.xfail(strict=True, reason="sync_members not written yet")


async def _instant(_seconds: float) -> None:
    """A sleep that returns at once."""


def _store() -> Any:
    """An in-memory store."""
    from github_statistics.store import Store

    return Store(Path(":memory:"))


def _members(*logins: str) -> list[dict[str, str]]:
    """A public members page, with the one key the collector reads."""
    return [{"login": login} for login in logins]


async def _sync(store: Any, orgs: list[str], owner_type: str = "Organization") -> Any:
    """Run the collector for target ``acme`` over ``orgs``."""
    from github_statistics.client import Transport
    from github_statistics.collect import sync_members

    async with Transport(None, sleep=_instant, max_retries=0) as transport:
        return await sync_members(
            transport, store, orgs, owner="acme", owner_type=owner_type, today=TODAY
        )


def _logins(store: Any, org: str) -> list[str]:
    """The stored public members of ``org``."""
    return [member.login for member in store.members(org)]


@respx.mock
async def test_reads_every_page() -> None:
    """Every page of the public members listing is stored."""
    url = f"{API}/orgs/acme/public_members"
    respx.get(url, params={"page": "2"}).respond(200, json=_members("carol"))
    first = respx.get(url).respond(
        200,
        json=_members("alice", "bob"),
        headers={"link": f'<{url}?per_page=100&page=2>; rel="next"'},
    )
    store = _store()

    counts = await _sync(store, ["acme"])

    assert first.calls[0].request.url.params["per_page"] == "100"
    assert counts == {"acme": 3}
    assert _logins(store, "acme") == ["alice", "bob", "carol"]


@respx.mock
async def test_replaces_the_stored_list() -> None:
    """A login no longer listed goes; one still listed keeps its first day."""
    respx.get(f"{API}/orgs/acme/public_members").respond(
        200, json=_members("alice", "dave")
    )
    store = _store()
    store.replace_members("acme", ["alice", "bob"], date(2026, 1, 1))

    await _sync(store, ["acme"])

    members = {member.login: member for member in store.members("acme")}
    assert sorted(members) == ["alice", "dave"]
    assert members["alice"].first_seen == date(2026, 1, 1)
    assert members["dave"].first_seen == TODAY


async def test_a_user_owner_makes_no_call() -> None:
    """A target owned by a user reads no member list for that owner."""
    store = _store()

    with respx.mock(assert_all_called=False) as router:
        route = router.route(url__startswith=API).respond(200, json=[])
        counts = await _sync(store, ["acme"], owner_type="User")

    assert counts == {}
    assert not route.called


@pytest.mark.parametrize("status", [404, 500])
@respx.mock
async def test_a_failure_keeps_the_stored_list(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed read keeps the stored list, warns and carries on."""
    respx.get(f"{API}/orgs/gone/public_members").respond(status, json={})
    respx.get(f"{API}/orgs/acme/public_members").respond(200, json=_members("eve"))
    store = _store()
    store.replace_members("gone", ["bob"], date(2026, 1, 1))

    with caplog.at_level(logging.WARNING):
        counts = await _sync(store, ["gone", "acme"])

    assert counts == {"acme": 1}
    assert _logins(store, "gone") == ["bob"]
    assert _logins(store, "acme") == ["eve"]
    assert "gone" in caplog.text


async def test_never_reads_the_private_member_list() -> None:
    """No request goes to ``/orgs/{org}/members``, whatever the token."""
    from github_statistics.client import Transport
    from github_statistics.collect import sync_members

    store = _store()
    with respx.mock(assert_all_called=False) as router:
        private = router.get(url__regex=rf"{API}/orgs/[^/]+/members(\?.*)?$")
        private.respond(200, json=_members("secret"))
        public = f"{API}/orgs/acme/public_members"
        router.get(public).respond(200, json=_members("alice"))
        router.get(f"{API}/orgs/other/public_members").respond(200, json=[])
        async with Transport("token", sleep=_instant, max_retries=0) as transport:
            await sync_members(
                transport,
                store,
                ["acme", "other"],
                owner="acme",
                owner_type="Organization",
                today=TODAY,
            )

    assert not private.called
    assert _logins(store, "acme") == ["alice"]


@respx.mock
async def test_a_spent_budget_keeps_the_stored_lists() -> None:
    """A spent budget ends the step and keeps every stored list."""
    from github_statistics.client import Budget, BudgetExhaustedError, Transport
    from github_statistics.collect import sync_members

    url = f"{API}/orgs/acme/public_members"
    respx.get(url).respond(
        200,
        json=_members("alice"),
        headers={"link": f'<{url}?per_page=100&page=2>; rel="next"'},
    )
    store = _store()
    store.replace_members("acme", ["bob"], date(2026, 1, 1))
    store.replace_members("other", ["carol"], date(2026, 1, 1))

    budget = Budget(rest=1)
    async with Transport(None, sleep=_instant, budget=budget) as transport:
        with pytest.raises(BudgetExhaustedError):
            await sync_members(
                transport,
                store,
                ["acme", "other"],
                owner="acme",
                owner_type="Organization",
                today=TODAY,
            )

    assert _logins(store, "acme") == ["bob"]
    assert _logins(store, "other") == ["carol"]
