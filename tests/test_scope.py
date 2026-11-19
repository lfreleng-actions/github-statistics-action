# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for scope resolution, applying it to the store, and the ``scope`` command."""

from __future__ import annotations

import importlib
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

API = "https://api.github.com"
pytestmark = pytest.mark.xfail(strict=True, reason="scope resolution not built yet")


def _repo(name: str, **extra: Any) -> dict[str, Any]:
    """A repository as the API returns it, with only the keys the resolver reads."""
    data = {
        "name": name,
        "full_name": f"acme/{name}",
        "visibility": "public",
        "private": False,
        "archived": False,
        "fork": False,
        "is_template": False,
        "created_at": "2020-01-02T03:04:05Z",
        "default_branch": "main",
    }
    data.update(extra)
    return data


def _api() -> Any:
    """The scope package, loaded when a test runs so a missing one fails that test."""
    return importlib.import_module("github_statistics.scope")


def _owner(login: str = "acme", kind: str = "Organization") -> None:
    """Mock ``GET /users/{login}``."""
    respx.get(f"{API}/users/{login}").respond(200, json={"login": login, "type": kind})


def _rate_limit(remaining: int = 60) -> None:
    """Mock the free ``GET /rate_limit`` the ``scope`` command seeds its budget from."""
    core = {"limit": 60, "remaining": remaining, "reset": 1_900_000_000}
    respx.get(f"{API}/rate_limit").respond(200, json={"resources": {"core": core}})


def _target(**extra: Any) -> Any:
    from github_statistics.config import Target

    return Target(owner="acme", **extra)


async def _resolve(target: Any, known: tuple[str, ...] = ()) -> Any:
    from github_statistics.client import Transport

    async def instant(_: float) -> None:
        return None

    async with Transport(None, sleep=instant, max_retries=0) as transport:
        return await _api().resolve_scope(transport, target, known=known)


def _names(scope: Any) -> list[str]:
    return [repository.name for repository in scope.repositories]


def _reasons(scope: Any) -> dict[str, str]:
    return {drop.name: drop.reason for drop in scope.dropped}


@respx.mock
async def test_organisation_listing_follows_pages_and_sorts() -> None:
    _owner()
    second = f"{API}/orgs/acme/repos?type=public&per_page=100&page=2"
    respx.get(f"{API}/orgs/acme/repos", params={"page": "2"}).respond(
        200, json=[_repo("alpha")]
    )
    first = respx.get(f"{API}/orgs/acme/repos").respond(
        200, json=[_repo("zeta")], headers={"Link": f'<{second}>; rel="next"'}
    )

    scope = await _resolve(_target())

    assert scope.owner == "acme"
    assert scope.owner_type == "Organization"
    assert _names(scope) == ["alpha", "zeta"]
    assert scope.repositories[0].created_at == "2020-01-02T03:04:05Z"
    assert scope.repositories[0].default_branch == "main"
    params = first.calls[0].request.url.params
    assert params["type"] == "public"
    assert params["per_page"] == "100"


@respx.mock
async def test_user_listing_drops_private_and_internal() -> None:
    _owner("jo", "User")
    route = respx.get(f"{API}/users/jo/repos").respond(
        200,
        json=[
            _repo("open"),
            _repo("secret", visibility="private", private=True),
            _repo("inside", visibility="internal"),
            _repo("odd", private=True),
        ],
    )
    from github_statistics.config import Target

    scope = await _resolve(Target(owner="jo"))

    assert route.calls[0].request.url.params["type"] == "owner"
    assert scope.owner_type == "User"
    assert _names(scope) == ["open"]
    assert _reasons(scope) == {
        "inside": "not public",
        "odd": "not public",
        "secret": "not public",
    }


@pytest.mark.parametrize(
    ("flag", "field", "reason", "default_kept"),
    [
        ("scan_archived", "archived", "archived", False),
        ("scan_forks", "fork", "fork", False),
        ("scan_templates", "is_template", "template", True),
    ],
)
@respx.mock
async def test_scan_toggles(
    flag: str, field: str, reason: str, default_kept: bool
) -> None:
    _owner()
    respx.get(f"{API}/orgs/acme/repos").respond(200, json=[_repo("x", **{field: True})])

    kept = await _resolve(_target(**{flag: True}))
    dropped = await _resolve(_target(**{flag: False}))
    default = await _resolve(_target())

    assert _names(kept) == ["x"]
    assert _reasons(dropped) == {"x": reason}
    assert bool(_names(default)) is default_kept


@respx.mock
async def test_include_and_exclude_globs_ignore_case() -> None:
    _owner()
    listing = [_repo("Tool-One"), _repo("tool-two"), _repo("Other")]
    respx.get(f"{API}/orgs/acme/repos").respond(200, json=listing)

    included = await _resolve(_target(include=("TOOL-*",)))
    excluded = await _resolve(_target(exclude=("*-ONE",)))

    assert _names(included) == ["Tool-One", "tool-two"]
    assert _reasons(included) == {"Other": "not included"}
    assert _names(excluded) == ["Other", "tool-two"]
    assert _reasons(excluded) == {"Tool-One": "excluded"}


@respx.mock
async def test_explicit_include_reads_each_name_without_listing() -> None:
    _owner()
    listing = respx.get(f"{API}/orgs/acme/repos").respond(200, json=[])
    respx.get(f"{API}/repos/acme/old").respond(200, json=_repo("old", archived=True))
    respx.get(f"{API}/repos/acme/gone").respond(404, json={})
    respx.get(f"{API}/repos/acme/secret").respond(
        200, json=_repo("secret", visibility="private", private=True)
    )
    respx.get(f"{API}/repos/acme/inside").respond(
        200, json=_repo("inside", visibility="internal")
    )

    scope = await _resolve(_target(include=("old", "gone", "secret", "inside")))

    assert not listing.called
    assert _names(scope) == ["old"]
    assert _reasons(scope) == {
        "gone": "not found",
        "inside": "not public",
        "secret": "not public",
    }


@respx.mock
async def test_empty_include_takes_the_listing_path() -> None:
    _owner()
    respx.get(f"{API}/orgs/acme/repos").respond(200, json=[_repo("a"), _repo("b")])

    scope = await _resolve(_target(include=()))

    assert _names(scope) == ["a", "b"]


@respx.mock
async def test_known_repositories_missing_from_the_listing_are_read() -> None:
    _owner()
    respx.get(f"{API}/orgs/acme/repos").respond(200, json=[_repo("a")])
    respx.get(f"{API}/repos/acme/hidden").respond(
        200, json=_repo("hidden", visibility="private", private=True)
    )
    respx.get(f"{API}/repos/acme/gone").respond(404, json={})
    respx.get(f"{API}/repos/acme/flaky").respond(500, json={})

    scope = await _resolve(_target(), known=("a", "hidden", "gone", "flaky"))

    assert _names(scope) == ["a"]
    assert _reasons(scope) == {"gone": "not found", "hidden": "not public"}
    assert scope.unresolved == ("flaky",)


@respx.mock
async def test_a_repository_transferred_to_another_owner_is_not_found() -> None:
    _owner()
    # The transport follows GitHub's redirect to the repository's new owner.
    respx.get(f"{API}/repos/acme/lib").respond(
        200, json=_repo("lib", full_name="other/lib")
    )

    scope = await _resolve(_target(include=("lib",)))

    assert _names(scope) == []
    assert _reasons(scope) == {"lib": "not found"}


def _scope(
    names: list[str], dropped: dict[str, str] | None = None, **extra: Any
) -> Any:
    api = _api()
    repositories = tuple(
        api.Repository(name, created, False, False, False, "main")
        for name, created in (item.split("@") for item in names)
    )
    drops = tuple(api.Dropped(name, reason) for name, reason in (dropped or {}).items())
    return api.ResolvedScope("acme", "Organization", repositories, drops, **extra)


TODAY = date(2026, 6, 1)
WINDOW = date(2025, 6, 2)


def test_apply_first_entry_starts_at_window_or_creation() -> None:
    from github_statistics.store import Store

    apply_scope = _api().apply_scope

    with Store(Path(":memory:")) as store:
        changes = apply_scope(
            store,
            _scope(["old@2019-01-01T00:00:00Z", "new@2026-01-15T10:00:00Z"]),
            today=TODAY,
            window_start=WINDOW,
        )

        assert changes.entered == ("acme/new", "acme/old")
        assert store.in_scope(WINDOW) == ["acme/old"]
        assert store.in_scope(date(2026, 1, 14)) == ["acme/old"]
        assert store.in_scope(date(2026, 1, 15)) == ["acme/new", "acme/old"]


def test_apply_leaving_and_returning() -> None:
    from github_statistics.store import Store

    apply_scope = _api().apply_scope

    with Store(Path(":memory:")) as store:
        store.enter_scope("acme/a", WINDOW)
        leave = apply_scope(
            store, _scope([], {"a": "archived"}), today=TODAY, window_start=WINDOW
        )
        later = date(2026, 6, 10)
        back = apply_scope(
            store, _scope(["a@2019-01-01T00:00:00Z"]), today=later, window_start=WINDOW
        )

        assert leave.left == ("acme/a",)
        assert back.entered == ("acme/a",)
        assert store.in_scope(date(2026, 5, 31)) == ["acme/a"]
        assert store.in_scope(TODAY) == []
        assert store.in_scope(date(2026, 6, 9)) == []
        assert store.in_scope(later) == ["acme/a"]


@pytest.mark.parametrize("reason", ["not public", "not found"])
def test_apply_purges_a_repository_that_turned_private(reason: str) -> None:
    from github_statistics.store import Store

    apply_scope = _api().apply_scope

    with Store(Path(":memory:")) as store:
        store.enter_scope("acme/a", WINDOW)
        changes = apply_scope(
            store, _scope([], {"a": reason}), today=TODAY, window_start=WINDOW
        )

        assert changes.purged == ("acme/a",)
        assert changes.left == ()
        assert store.in_scope(WINDOW) == []


def test_apply_purges_every_spelling_of_a_repository_name() -> None:
    from github_statistics.store import Store

    apply_scope = _api().apply_scope

    with Store(Path(":memory:")) as store:
        store.enter_scope("acme/lib", WINDOW)
        store.leave_scope("acme/lib", date(2026, 1, 1))
        changes = apply_scope(
            store, _scope([], {"LIB": "not found"}), today=TODAY, window_start=WINDOW
        )

        assert changes.purged == ("acme/LIB",)
        assert store.stored_repositories() == []


def test_apply_purges_a_repository_known_only_by_retained_rows() -> None:
    from github_statistics.store import Store
    from github_statistics.store.models import SyncState

    apply_scope = _api().apply_scope

    with Store(Path(":memory:")) as store:
        store.set_sync_state(SyncState(repo="acme/lib", metric="issues"))
        changes = apply_scope(
            store, _scope([], {"LIB": "not found"}), today=TODAY, window_start=WINDOW
        )

        assert changes.purged == ("acme/LIB",)
        assert store.sync_state("acme/lib", "issues") is None


def test_apply_return_after_prune_enters_from_today() -> None:
    from github_statistics.store import Store

    apply_scope = _api().apply_scope

    with Store(Path(":memory:")) as store:
        store.enter_scope("acme/a", WINDOW)
        apply_scope(
            store, _scope([], {"a": "archived"}), today=TODAY, window_start=WINDOW
        )
        later, later_window = date(2027, 9, 1), date(2026, 9, 2)
        store.prune(later_window)
        back = apply_scope(
            store,
            _scope(["a@2019-01-01T00:00:00Z"]),
            today=later,
            window_start=later_window,
        )

        assert back.entered == ("acme/a",)
        assert store.in_scope(later_window) == []
        assert store.in_scope(later) == ["acme/a"]


def test_apply_same_day_exit_then_return_enters_from_today() -> None:
    from github_statistics.store import Store

    apply_scope = _api().apply_scope

    with Store(Path(":memory:")) as store:
        apply_scope(
            store, _scope(["a@2026-06-01T08:00:00Z"]), today=TODAY, window_start=WINDOW
        )
        apply_scope(
            store, _scope([], {"a": "archived"}), today=TODAY, window_start=WINDOW
        )
        later = date(2026, 6, 10)
        back = apply_scope(
            store, _scope(["a@2026-06-01T08:00:00Z"]), today=later, window_start=WINDOW
        )

        assert back.entered == ("acme/a",)
        assert store.in_scope(date(2026, 6, 9)) == []
        assert store.in_scope(later) == ["acme/a"]


@respx.mock
async def test_stored_keys_resolve_and_purge_a_repository_that_turned_private() -> None:
    from github_statistics.store import Store

    _owner()
    respx.get(f"{API}/orgs/acme/repos").respond(200, json=[_repo("a")])
    respx.get(f"{API}/repos/acme/lib").respond(
        200, json=_repo("lib", visibility="private", private=True)
    )

    with Store(Path(":memory:")) as store:
        store.enter_scope("acme/a", WINDOW)
        store.enter_scope("acme/lib", WINDOW)
        store.enter_scope("other/x", WINDOW)
        scope = await _resolve(_target(), known=tuple(store.stored_repositories()))
        changes = _api().apply_scope(store, scope, today=TODAY, window_start=WINDOW)

        assert _reasons(scope) == {"lib": "not public"}
        assert changes.purged == ("acme/lib",)
        assert store.stored_repositories() == ["acme/a", "other/x"]


def test_apply_leaves_an_undetermined_repository_untouched() -> None:
    from github_statistics.store import Store

    apply_scope = _api().apply_scope

    with Store(Path(":memory:")) as store:
        store.enter_scope("acme/a", WINDOW)
        store.enter_scope("acme/b", WINDOW)
        changes = apply_scope(
            store, _scope([], unresolved=("a",)), today=TODAY, window_start=WINDOW
        )

        assert changes.untouched == ("acme/a", "acme/b")
        assert store.in_scope(TODAY) == ["acme/a", "acme/b"]


runner = CliRunner()


@pytest.fixture
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's own configuration and token out of the command."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("XDG_CONFIG_HOME", "GHS_CONFIG", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _invoke(*args: str) -> Any:
    from github_statistics.cli import app

    return runner.invoke(app, ["scope", *args])


@pytest.mark.usefixtures("_isolated")
@pytest.mark.parametrize(
    ("output", "expected"),
    [("lines", "a\nb\n"), ("csv", "a,b\n"), ("json", '["a", "b"]\n')],
)
@respx.mock
def test_scope_command_formats(output: str, expected: str) -> None:
    _rate_limit()
    _owner()
    respx.get(f"{API}/orgs/acme/repos").respond(
        200, json=[_repo("b"), _repo("a"), _repo("s", visibility="private")]
    )

    result = _invoke("--org", "acme", "--format", output)

    assert result.exit_code == 0, result.output
    assert result.stdout == expected
    assert "acme/s: not public" in result.stderr


@pytest.mark.usefixtures("_isolated")
@respx.mock
def test_scope_command_empty_prints_nothing() -> None:
    _rate_limit()
    _owner()
    respx.get(f"{API}/orgs/acme/repos").respond(200, json=[])

    result = _invoke("--org", "acme", "--format", "json")

    assert result.exit_code == 0
    assert result.stdout == ""
    assert "no public repositories" in result.stderr


@pytest.mark.usefixtures("_isolated")
@respx.mock
def test_scope_command_repo_flags_read_each_repository() -> None:
    _rate_limit()
    _owner()
    listing = respx.get(f"{API}/orgs/acme/repos").respond(200, json=[])
    respx.get(f"{API}/repos/acme/a").respond(200, json=_repo("a"))
    respx.get(f"{API}/repos/acme/b").respond(200, json=_repo("b"))

    result = _invoke("--repo", "acme/b", "--repo", "acme/a")

    assert result.exit_code == 0, result.output
    assert result.stdout == "a\nb\n"
    assert not listing.called


@pytest.mark.usefixtures("_isolated")
@pytest.mark.parametrize("readable", [(), ("a",)], ids=["all-failed", "some-failed"])
@respx.mock
def test_scope_command_fails_when_a_repository_cannot_be_read(
    readable: tuple[str, ...],
) -> None:
    _rate_limit()
    _owner()
    for name in ("a", "b"):
        route = respx.get(f"{API}/repos/acme/{name}")
        if name in readable:
            route.respond(200, json=_repo(name))
        else:
            route.respond(451, json={})

    result = _invoke("--repo", "acme/a", "--repo", "acme/b")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "could not read acme/b" in result.stderr
    assert "scope of acme is incomplete" in result.stderr


@pytest.mark.usefixtures("_isolated")
@respx.mock
def test_scope_command_reports_an_unreadable_owner() -> None:
    _rate_limit()
    respx.get(f"{API}/users/acme").respond(404, json={})

    result = _invoke("--org", "acme")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "cannot resolve the scope of acme" in result.stderr


@pytest.mark.usefixtures("_isolated")
@respx.mock
def test_scope_command_spends_from_the_configured_budget() -> None:
    _rate_limit()
    _owner()
    listing = respx.get(f"{API}/orgs/acme/repos").respond(200, json=[_repo("a")])

    result = _invoke("--org", "acme", "--budget", "1")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert not listing.called
