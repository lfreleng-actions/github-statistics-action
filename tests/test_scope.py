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


YESTERDAY = date(2026, 5, 31)
ITEM_METRICS = ("pull_requests", "issues")
WORKFLOW_METRICS = (*ITEM_METRICS, "workflows")


def _cover(store: Any, repo: str, metrics: tuple[str, ...] = ITEM_METRICS) -> None:
    """Cover ``metrics`` of ``repo`` over the whole window."""
    for metric in metrics:
        store.add_coverage(repo, metric, WINDOW, YESTERDAY)


def _run(repo: str, day: date, jobs: int | None) -> Any:
    from datetime import UTC, datetime, time

    from github_statistics.store import Run

    created = datetime.combine(day, time(12), tzinfo=UTC)
    return Run(
        1,
        "R_1",
        repo,
        "ci.yaml",
        "push",
        "completed",
        "success",
        created,
        "alice",
        "User",
        1,
        jobs,
    )


def _sync_state(repo: str) -> Any:
    from github_statistics.store import SyncState

    return SyncState(repo=repo, metric="items")


@pytest.mark.xfail(strict=True)
def test_apply_keeps_a_repository_that_left_complete() -> None:
    from github_statistics.store import Store

    with Store(Path(":memory:")) as store:
        store.enter_scope("acme/a", WINDOW)
        _cover(store, "acme/a")
        changes = _api().apply_scope(
            store,
            _scope([], {"a": "archived"}),
            today=TODAY,
            window_start=WINDOW,
            metrics=ITEM_METRICS,
        )

        assert changes.left == ("acme/a",)
        assert changes.purged == ()
        assert store.in_scope(YESTERDAY) == ["acme/a"]
        assert store.in_scope(TODAY) == []
        assert store.coverage("acme/a", "issues")


@pytest.mark.xfail(strict=True)
def test_apply_purges_a_repository_that_left_incomplete(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from github_statistics.collect.run import window_coverage
    from github_statistics.store import Store

    with Store(Path(":memory:")) as store:
        for repo in ("acme/a", "acme/b"):
            store.enter_scope(repo, WINDOW)
        _cover(store, "acme/b")
        store.add_coverage("acme/a", "issues", WINDOW, date(2026, 1, 1))
        with caplog.at_level("INFO", logger="github_statistics.scope.apply"):
            changes = _api().apply_scope(
                store,
                _scope(["b@2019-01-01T00:00:00Z"], {"a": "archived"}),
                today=TODAY,
                window_start=WINDOW,
                metrics=ITEM_METRICS,
            )
        _, gaps = window_coverage(store, "acme", list(ITEM_METRICS), WINDOW, YESTERDAY)

        assert changes.purged == ("acme/a",)
        assert changes.left == ()
        assert changes.reasons == {"acme/a": "left incomplete"}
        assert store.stored_repositories() == ["acme/b"]
        assert gaps == 0
        assert "purged acme/a: left incomplete" in caplog.text


@pytest.mark.xfail(strict=True)
def test_apply_purges_a_repository_that_left_incomplete_on_an_earlier_run() -> None:
    from github_statistics.store import Store

    with Store(Path(":memory:")) as store:
        store.enter_scope("acme/A", WINDOW)
        store.leave_scope("acme/A", date(2026, 1, 1))
        store.set_sync_state(_sync_state("acme/a"))
        store.enter_scope("acme/b", WINDOW)
        _cover(store, "acme/b")
        store.add_coverage("acme/A", "issues", WINDOW, date(2025, 12, 1))
        changes = _api().apply_scope(
            store,
            _scope(["b@2019-01-01T00:00:00Z"]),
            today=TODAY,
            window_start=WINDOW,
            metrics=ITEM_METRICS,
        )

        assert changes.purged == ("acme/A",)
        assert store.stored_repositories() == ["acme/b"]


@pytest.mark.xfail(strict=True)
def test_apply_keeps_a_departed_repository_uncovered_only_before_the_horizon() -> None:
    from github_statistics.store import Store

    horizon = date(2025, 7, 1)
    with Store(Path(":memory:")) as store:
        for repo in ("acme/a", "acme/b"):
            store.enter_scope(repo, WINDOW)
            _cover(store, repo)
        store.add_coverage("acme/a", "runs", horizon, YESTERDAY)
        store.add_coverage("acme/b", "runs", WINDOW, YESTERDAY)
        store.upsert_runs([_run("acme/b", horizon, 3)])
        changes = _api().apply_scope(
            store,
            _scope(["b@2019-01-01T00:00:00Z"], {"a": "archived"}),
            today=TODAY,
            window_start=WINDOW,
            metrics=WORKFLOW_METRICS,
        )

        assert changes.left == ("acme/a",)
        assert changes.purged == ()
        assert store.coverage("acme/a", "runs")


@pytest.mark.xfail(strict=True)
@pytest.mark.parametrize(
    ("charted", "purged"),
    [(WORKFLOW_METRICS, ("acme/a",)), ((*ITEM_METRICS, "workflow_outcomes"), ())],
)
def test_apply_grades_missing_job_counts_only_for_workflows(
    charted: tuple[str, ...], purged: tuple[str, ...]
) -> None:
    from github_statistics.store import Store

    with Store(Path(":memory:")) as store:
        for repo in ("acme/a", "acme/b"):
            store.enter_scope(repo, WINDOW)
            _cover(store, repo, (*ITEM_METRICS, "runs"))
        store.upsert_runs([_run("acme/a", date(2026, 3, 1), None)])
        changes = _api().apply_scope(
            store,
            _scope(["b@2019-01-01T00:00:00Z"], {"a": "archived"}),
            today=TODAY,
            window_start=WINDOW,
            metrics=charted,
        )

        assert changes.purged == purged


@pytest.mark.xfail(strict=True)
def test_apply_grades_only_through_the_window_end() -> None:
    from github_statistics.store import Store

    end = date(2026, 2, 28)
    with Store(Path(":memory:")) as store:
        store.enter_scope("acme/a", WINDOW)
        for metric in ITEM_METRICS:
            store.add_coverage("acme/a", metric, WINDOW, end)
        changes = _api().apply_scope(
            store,
            _scope([], {"a": "archived"}),
            today=TODAY,
            window_start=WINDOW,
            window_end=end,
            metrics=ITEM_METRICS,
        )

        assert changes.left == ("acme/a",)
        assert changes.purged == ()


@pytest.mark.xfail(strict=True)
def test_apply_purges_an_unfinished_first_listing_with_no_coverage_held() -> None:
    from github_statistics.store import Store

    with Store(Path(":memory:")) as store:
        for repo in ("acme/a", "acme/b"):
            store.enter_scope(repo, WINDOW)
        store.set_sync_state(_sync_state("acme/a"))
        changes = _api().apply_scope(
            store,
            _scope(["b@2019-01-01T00:00:00Z"], {"a": "archived"}),
            today=TODAY,
            window_start=WINDOW,
            metrics=ITEM_METRICS,
        )

        assert changes.purged == ("acme/a",)
        assert store.stored_repositories() == ["acme/b"]


@pytest.mark.xfail(strict=True)
def test_apply_grades_every_departed_repository_before_purging() -> None:
    from github_statistics.store import Store

    with Store(Path(":memory:")) as store:
        for repo in ("acme/a", "acme/b"):
            store.enter_scope(repo, WINDOW)
        store.add_coverage("acme/a", "issues", WINDOW, date(2026, 1, 1))
        changes = _api().apply_scope(
            store,
            _scope([], {"a": "archived", "b": "archived"}),
            today=TODAY,
            window_start=WINDOW,
            metrics=ITEM_METRICS,
        )

        assert changes.purged == ("acme/a", "acme/b")
        assert store.stored_repositories() == []


@pytest.mark.xfail(strict=True)
def test_apply_reads_run_history_once_for_every_departed_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from github_statistics.store import Store

    calls: dict[str, int] = {"earliest_run_day": 0, "days_missing_jobs": 0}
    for name in calls:
        query = getattr(Store, name)

        def counted(self: Any, *args: Any, _name: str = name, _q: Any = query) -> Any:
            calls[_name] += 1
            return _q(self, *args)

        monkeypatch.setattr(Store, name, counted)
    with Store(Path(":memory:")) as store:
        for repo in ("acme/a", "acme/b", "acme/c"):
            store.enter_scope(repo, WINDOW)
            _cover(store, repo, (*ITEM_METRICS, "runs"))
        changes = _api().apply_scope(
            store,
            _scope([], {"a": "archived", "b": "archived", "c": "archived"}),
            today=TODAY,
            window_start=WINDOW,
            metrics=WORKFLOW_METRICS,
        )

        assert changes.left == ("acme/a", "acme/b", "acme/c")
        assert calls == {"earliest_run_day": 2, "days_missing_jobs": 1}


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
