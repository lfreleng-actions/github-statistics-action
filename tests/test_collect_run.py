# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the budgeted sync that orders the collectors (design §6.5)."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

API = "https://api.github.com"
NOW = datetime(2026, 3, 10, 6, tzinfo=UTC)
WINDOW = date(2026, 3, 7)
YESTERDAY = date(2026, 3, 9)


def _run() -> Any:
    """The orchestration module, loaded when a test runs."""
    return importlib.import_module("github_statistics.collect.run")


async def _instant(_seconds: float) -> None:
    """A sleep that returns at once."""


def _store() -> Any:
    """An in-memory store."""
    from github_statistics.store import Store

    return Store(Path(":memory:"))


def _config(**metrics: bool) -> Any:
    """A three-day window over target ``acme``, with ``metrics`` overridden."""
    from github_statistics.config import Config, Target
    from github_statistics.config.models import Metrics, Window

    return Config(
        targets=(Target("acme"),), window=Window(days=3), metrics=Metrics(**metrics)
    )


def _scope(*names: str, owner_type: str = "Organization") -> Any:
    """A resolved scope of ``acme`` holding ``names``."""
    from github_statistics.scope import Repository, ResolvedScope

    repositories = tuple(
        Repository(name, "2020-01-01T00:00:00Z", False, False, False, "main")
        for name in names
    )
    return ResolvedScope("acme", owner_type, repositories, ())


class _Fakes:
    """Stub collectors recording each call, in order, in ``calls``."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, scope: Any, **unreadable: Any
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.scope = scope
        self.unreadable = unreadable
        self.spend = 0
        self.stop: str | None = None
        self.backfilled = 1
        self.window_starts: list[Any] = []
        run = _run()
        fakes = self

        class RunSync:
            def __init__(self, _t: Any, _s: Any, key: str, **_kw: Any) -> None:
                self.key = key
                self.runs = 0

            async def guarded(self, phase: Any) -> bool:
                await phase()
                return fakes.stop != "runs"

            async def recent(self) -> None:
                fakes.calls.append(("runs.recent", self.key))

            async def backfill(self) -> None:
                fakes.calls.append(("runs.backfill", self.key))
                self.runs += fakes.backfilled

            def result(self) -> Any:
                from github_statistics.collect import RunSyncResult

                status = fakes.unreadable.get("runs")
                stalled = fakes.unreadable.get("stalled") == self.key
                return RunSyncResult(
                    self.key, self.runs, 0, True, False, status, stalled
                )

        monkeypatch.setattr(run, "_RunSync", RunSync)
        for name in (
            "resolve_scope",
            "sync_members",
            "sync_all_items",
            "enrich_items",
            "enrich_jobs",
            "sweep_runs",
        ):
            monkeypatch.setattr(run, name, getattr(self, name))

    async def resolve_scope(self, transport: Any, _target: Any, **_kw: Any) -> Any:
        self.calls.append(("scope",))
        return self.scope

    async def sync_members(self, _t: Any, _s: Any, orgs: Iterable[str], **_kw: Any):
        self.calls.append(("members", *orgs))
        if self.stop == "members":
            from github_statistics.client import BudgetExhaustedError

            raise BudgetExhaustedError("the core rate limit is spent")
        return {}

    async def sync_all_items(self, transport: Any, _s: Any, scope: Any, **_kw: Any):
        from github_statistics.collect import ItemSyncResult

        keys = tuple(f"acme/{repository.name}" for repository in scope.repositories)
        self.window_starts.append(_kw["window_start"])
        self.calls.append(("items.start", *keys))
        await asyncio.sleep(0)
        for _ in range(self.spend):
            transport.budget.try_spend_rest()
        self.calls.append(("items.end", *keys))
        status = self.unreadable.get("items")
        drained = self.stop != "items"
        return {key: ItemSyncResult(key, 0, 0, drained, status) for key in keys}

    async def enrich_items(self, _t: Any, _s: Any, repos: Iterable[str]) -> Any:
        self.calls.append(("enrich_items", *repos))

    async def enrich_jobs(self, _t: Any, _s: Any, repos: Iterable[str]) -> Any:
        self.calls.append(("enrich_jobs", *repos))

    async def sweep_runs(self, _t: Any, _s: Any, owner: str, repo: str, **_kw: Any):
        from github_statistics.collect import SweepResult

        self.calls.append(("sweep", f"{owner}/{repo}"))
        return SweepResult(f"{owner}/{repo}", 0, 0, True, self.stop != "sweep")


async def _collect(store: Any, config: Any = None, **kwargs: Any) -> Any:
    """Run ``collect`` for ``acme`` on an unbudgeted, offline transport."""
    from github_statistics.client import Budget, Transport

    config = config or _config()
    budget = kwargs.pop("budget", Budget())
    async with Transport(None, sleep=_instant, budget=budget) as transport:
        return await _run().collect(
            config,
            config.targets[0],
            now=NOW,
            transport=transport,
            store=store,
            **kwargs,
        )


def _watermark(store: Any, key: str) -> None:
    """Give ``key`` an item watermark, as an earlier sync leaves."""
    from github_statistics.store import SyncState

    store.set_sync_state(SyncState(key, "items", watermark=NOW, oldest=NOW))


@pytest.mark.xfail(strict=True, reason="no second enrichment pass yet")
async def test_runs_the_steps_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scope, members, incremental, enrichment, backfill, enrichment, sweep."""
    store = _store()
    _watermark(store, "acme/a")
    fakes = _Fakes(monkeypatch, _scope("a", "b"))

    await _collect(store)

    assert fakes.calls == [
        ("scope",),
        ("members", "acme"),
        ("items.start", "acme/a"),
        ("items.end", "acme/a"),
        ("runs.recent", "acme/a"),
        ("runs.recent", "acme/b"),
        ("enrich_items", "acme/a", "acme/b"),
        ("enrich_jobs", "acme/a", "acme/b"),
        ("items.start", "acme/b"),
        ("items.end", "acme/b"),
        ("runs.backfill", "acme/a"),
        ("runs.backfill", "acme/b"),
        ("enrich_items", "acme/b"),
        ("enrich_jobs", "acme/a", "acme/b"),
        ("sweep", "acme/a"),
        ("sweep", "acme/b"),
    ]


async def test_item_listing_ends_before_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repository's listing sync completes before its enrichment starts."""
    store = _store()
    _watermark(store, "acme/a")
    fakes = _Fakes(monkeypatch, _scope("a"))

    await _collect(store)

    names = [call[0] for call in fakes.calls]
    assert names.index("items.end") < names.index("enrich_items")
    assert names.count("items.start") == 1


async def test_a_small_budget_still_runs_incremental_and_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget spent in step 3 still enriches, and reports where it ran out."""
    from github_statistics.client import Budget

    store = _store()
    _watermark(store, "acme/a")
    fakes = _Fakes(monkeypatch, _scope("a"))
    fakes.spend = 5

    result = await _collect(store, budget=Budget(rest=5))

    names = [call[0] for call in fakes.calls]
    assert "runs.recent" in names
    assert "enrich_items" in names
    assert "enrich_jobs" in names
    assert result.exhausted == "incremental"


@pytest.mark.xfail(strict=True, reason="no second enrichment pass yet")
async def test_a_first_sync_enriches_its_items_in_the_same_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items a repository's first sync stores are enriched before the run ends."""
    store = _store()
    fakes = _Fakes(monkeypatch, _scope("a"))

    await _collect(store, _config(workflows=False))

    assert fakes.calls[-3:] == [
        ("items.start", "acme/a"),
        ("items.end", "acme/a"),
        ("enrich_items", "acme/a"),
    ]


async def test_a_warm_run_enriches_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing to backfill, each enrichment runs once."""
    store = _store()
    _watermark(store, "acme/a")
    fakes = _Fakes(monkeypatch, _scope("a"))
    fakes.backfilled = 0

    await _collect(store)

    names = [call[0] for call in fakes.calls]
    assert names.count("enrich_items") == 1
    assert names.count("enrich_jobs") == 1


@pytest.mark.xfail(strict=True, reason="no second enrichment pass yet")
async def test_a_budget_spent_in_backfill_still_enriches_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REST budget the backfill spends leaves the GraphQL pass to run."""
    from github_statistics.client import Budget

    store = _store()
    fakes = _Fakes(monkeypatch, _scope("a"))
    fakes.spend = 5

    result = await _collect(store, budget=Budget(rest=5))

    assert result.exhausted == "backfill"
    backfill = fakes.calls.index(("runs.backfill", "acme/a"))
    assert fakes.calls[backfill + 1 :] == [
        ("enrich_items", "acme/a"),
        ("enrich_jobs", "acme/a"),
        ("sweep", "acme/a"),
    ]


async def test_a_spent_scope_budget_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget the scope resolution spends ends the run without an error."""
    from github_statistics.client import BudgetExhaustedError

    store = _store()
    fakes = _Fakes(monkeypatch, _scope("a"))

    async def spent(*_args: Any, **_kw: Any) -> Any:
        raise BudgetExhaustedError("no REST budget left")

    monkeypatch.setattr(_run(), "resolve_scope", spent)

    result = await _collect(store)

    assert result.exhausted == "scope"
    assert fakes.calls == []


@pytest.mark.parametrize(
    ("stop", "step"),
    [
        ("members", "members"),
        ("items", "incremental"),
        ("runs", "incremental"),
        ("sweep", "sweep"),
    ],
)
async def test_a_collector_stop_reports_its_step(
    monkeypatch: pytest.MonkeyPatch, stop: str, step: str
) -> None:
    """A rate limit a collector stops at is reported with budget to spare."""
    store = _store()
    _watermark(store, "acme/a")
    fakes = _Fakes(monkeypatch, _scope("a"))
    fakes.stop = stop

    result = await _collect(store)

    assert result.exhausted == step
    assert result.unreadable == {}
    assert ("enrich_items", "acme/a") in fakes.calls


async def test_refresh_clears_watermarks_and_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``refresh`` drops the scope's sync state and coverage, not its rows."""
    from github_statistics.store import Item

    store = _store()
    _watermark(store, "acme/a")
    store.add_coverage("acme/a", "issues", WINDOW, YESTERDAY)
    item = Item("acme/a", 1, "I_1", "issue", NOW, None, None, "alice", "User", NOW)
    store.upsert_items([item])
    fakes = _Fakes(monkeypatch, _scope("a"))

    await _collect(store, refresh=True)

    assert store.sync_state("acme/a", "items") is None
    assert store.coverage("acme/a", "issues") == []
    assert store.counts()["items"] == 1
    starts = [call for call in fakes.calls if call[0] == "items.start"]
    assert starts == [("items.start", "acme/a")]
    assert fakes.calls.index(starts[0]) > fakes.calls.index(("enrich_items", "acme/a"))


@respx.mock
async def test_an_empty_scope_makes_no_further_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty scope still purges what left it, and calls nothing else."""
    store = _store()
    store.enter_scope("acme/old", date(2026, 1, 1))
    store.add_coverage("acme/old", "issues", WINDOW, YESTERDAY)
    respx.get(f"{API}/users/acme").respond(
        200, json={"login": "acme", "type": "Organization"}
    )
    respx.get(f"{API}/orgs/acme/repos").respond(200, json=[])
    respx.get(f"{API}/repos/acme/old").respond(404, json={})
    fakes = _Fakes(monkeypatch, None)
    monkeypatch.setattr(_run(), "resolve_scope", _real_resolve_scope())

    result = await _collect(store)

    assert fakes.calls == []
    assert store.stored_repositories() == []
    assert result.dropped == {"acme/old": "not found"}


@respx.mock
@pytest.mark.parametrize("status", [500, 503])
@pytest.mark.parametrize("failing", ["owner", "listing"])
async def test_a_rest_server_error_ends_the_scope_step(
    monkeypatch: pytest.MonkeyPatch, failing: str, status: int
) -> None:
    """A REST ``5xx`` past the retries in the scope read ends that step."""
    store = _store()
    owner = respx.get(f"{API}/users/acme")
    if failing == "owner":
        owner.respond(status, json={})
    else:
        owner.respond(200, json={"login": "acme", "type": "Organization"})
        respx.get(f"{API}/orgs/acme/repos").respond(status, json={})
    fakes = _Fakes(monkeypatch, None)
    monkeypatch.setattr(_run(), "resolve_scope", _real_resolve_scope())

    result = await _collect(store)

    assert fakes.calls == []
    assert result.owner == "acme"
    assert result.exhausted is None


@respx.mock
async def test_a_rest_client_error_in_the_scope_read_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REST error that is not a server error, such as a ``404``, propagates."""
    store = _store()
    respx.get(f"{API}/users/acme").respond(404, json={})
    _Fakes(monkeypatch, None)
    monkeypatch.setattr(_run(), "resolve_scope", _real_resolve_scope())

    with pytest.raises(httpx.HTTPStatusError):
        await _collect(store)


def _real_resolve_scope() -> Any:
    """The scope resolver itself, unstubbed."""
    from github_statistics.scope import resolve_scope

    return resolve_scope


async def test_disabled_metrics_skip_their_collectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With workflows off, no run, job or sweep collector runs."""
    store = _store()
    fakes = _Fakes(monkeypatch, _scope("a"))

    await _collect(store, _config(workflows=False))

    names = {call[0] for call in fakes.calls}
    assert names == {"scope", "members", "enrich_items", "items.start", "items.end"}


async def test_reports_unreadable_and_stalled_repositories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item and run failures and a stalled read reach the result."""
    store = _store()
    _watermark(store, "acme/a")
    _Fakes(monkeypatch, _scope("a", "b"), items=404, runs=502, stalled="acme/b")

    result = await _collect(store)

    assert result.unreadable == {
        "acme/a": "items: HTTP 404",
        "acme/b": "items: HTTP 404",
    }
    assert result.stalled == ("acme/b",)


async def test_reports_window_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Days complete, partial and missing per enabled metric, and gap days."""
    store = _store()
    _Fakes(monkeypatch, _scope("a"))
    store.enter_scope("acme/a", date(2026, 1, 1))
    for metric in ("issues", "pull_requests"):
        store.add_coverage("acme/a", metric, WINDOW, YESTERDAY)

    result = await _collect(store, _config(workflows=False))

    from github_statistics.collect import MetricCoverage

    assert result.coverage == {
        "pull_requests": MetricCoverage(3, 0, 0),
        "issues": MetricCoverage(3, 0, 0),
    }
    assert result.gap_days == 0
    assert result.coverage_complete


async def test_a_rejected_token_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``AuthError`` from any step reaches the caller as itself."""
    from github_statistics.client import AuthError

    store = _store()
    _Fakes(monkeypatch, _scope("a"))

    async def rejected(*_args: Any, **_kw: Any) -> Any:
        raise AuthError("401")

    monkeypatch.setattr(_run(), "sync_all_items", rejected)

    with pytest.raises(AuthError):
        await _collect(store)


@pytest.mark.parametrize("collector", ["enrich_items", "enrich_jobs"])
@pytest.mark.parametrize("status", [500, 503])
async def test_a_graphql_server_error_ends_its_step(
    monkeypatch: pytest.MonkeyPatch, collector: str, status: int
) -> None:
    """A GraphQL ``5xx`` past the retries ends enrichment; later steps run."""
    from github_statistics.client import GraphQLError

    store = _store()
    _watermark(store, "acme/a")
    fakes = _Fakes(monkeypatch, _scope("a"))

    async def outage(*_args: Any, **_kw: Any) -> Any:
        raise GraphQLError(f"GraphQL HTTP {status}", status=status)

    monkeypatch.setattr(_run(), collector, outage)

    result = await _collect(store)

    names = [call[0] for call in fakes.calls]
    assert "runs.backfill" in names
    assert "sweep" in names
    assert result.exhausted is None


@pytest.mark.parametrize("status", [None, 400])
async def test_a_refused_graphql_query_propagates(
    monkeypatch: pytest.MonkeyPatch, status: int | None
) -> None:
    """A GraphQL error that is not a server error reaches the caller."""
    from github_statistics.client import GraphQLError

    store = _store()
    _Fakes(monkeypatch, _scope("a"))

    async def refused(*_args: Any, **_kw: Any) -> Any:
        raise GraphQLError("GraphQL query failed", status=status)

    monkeypatch.setattr(_run(), "enrich_items", refused)

    with pytest.raises(GraphQLError):
        await _collect(store)


async def test_until_ends_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """``until`` sets the window read and reported, not ``now``."""
    store = _store()
    fakes = _Fakes(monkeypatch, _scope("a"))
    store.enter_scope("acme/a", date(2026, 1, 1))
    for metric in ("issues", "pull_requests"):
        store.add_coverage("acme/a", metric, date(2026, 2, 26), date(2026, 2, 28))

    result = await _collect(store, _config(workflows=False), until=date(2026, 2, 28))

    assert fakes.window_starts == [date(2026, 2, 26)]
    assert result.gap_days == 0


async def test_an_outage_marks_its_repository_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An item read that outlasts its retries makes the repository unreadable."""
    store = _store()
    _Fakes(monkeypatch, _scope("a"))
    items = importlib.import_module("github_statistics.collect.items")
    monkeypatch.setattr(_run(), "sync_all_items", items.sync_all_items)

    with respx.mock:
        respx.get(f"{API}/repos/acme/a/issues").mock(side_effect=httpx.ConnectTimeout)
        result = await _collect(store, _config(workflows=False))

    assert list(result.unreadable) == ["acme/a"]
    assert result.unreadable["acme/a"].startswith("items: GET ")


async def _outage_after(ticks: int) -> None:
    """Yield ``ticks`` times, then fail as a read past its retries does."""
    from github_statistics.client import TransportError

    for _ in range(ticks):
        await asyncio.sleep(0)
    raise TransportError("GET /repos/acme/b: timed out")


async def test_a_sibling_outage_keeps_finished_run_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repository whose recent runs finished goes on to backfill and sweep."""
    store = _store()
    fakes = _Fakes(monkeypatch, _scope("a", "b"))
    run = _run()
    base = run._RunSync

    class RunSync(base):  # type: ignore[misc, valid-type]
        async def recent(self) -> None:
            await super().recent()
            if self.key == "acme/b":
                await _outage_after(3)

    monkeypatch.setattr(run, "_RunSync", RunSync)

    result = await _collect(store, _config(issues=False, pull_requests=False))

    assert ("runs.backfill", "acme/a") in fakes.calls
    assert ("sweep", "acme/a") in fakes.calls
    assert ("runs.backfill", "acme/b") not in fakes.calls
    assert list(result.unreadable) == ["acme/b"]
    assert result.exhausted is None


async def test_a_sibling_outage_keeps_finished_item_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An item sync that finished reports its unreadable repository."""
    from github_statistics.collect import ItemSyncResult

    store = _store()
    _watermark(store, "acme/a")
    _watermark(store, "acme/b")
    _Fakes(monkeypatch, _scope("a", "b"))

    async def items(_t: Any, _s: Any, scope: Any, **_kw: Any) -> Any:
        key = f"acme/{scope.repositories[0].name}"
        if key == "acme/b":
            await _outage_after(3)
        return {key: ItemSyncResult(key, 0, 0, True, 404)}

    monkeypatch.setattr(_run(), "sync_all_items", items)

    result = await _collect(store, _config(workflows=False))

    assert result.unreadable["acme/a"] == "items: HTTP 404"
    assert result.unreadable["acme/b"].startswith("items: GET ")


def _forbidden(_request: httpx.Request) -> httpx.Response:
    """A GraphQL answer refusing every node it was asked for."""
    error = {"type": "FORBIDDEN", "message": "Resource not accessible"}
    return httpx.Response(200, json={"data": {"nodes": [None]}, "errors": [error]})


async def test_an_item_enrichment_failure_makes_its_repository_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An item the real enrichment cannot read reaches ``unreadable``."""
    from github_statistics.collect import enrich_items
    from github_statistics.store import Item

    store = _store()
    _Fakes(monkeypatch, _scope("a", "b"))
    monkeypatch.setattr(_run(), "enrich_items", enrich_items)
    store.upsert_items(
        [
            Item(
                repo="acme/a",
                number=1,
                node_id="I_1",
                kind="issue",
                created_at=NOW,
                closed_at=None,
                merged_at=None,
                author="alice",
                author_type="User",
                updated_at=NOW,
            )
        ]
    )

    with respx.mock:
        respx.post(f"{API}/graphql").mock(side_effect=_forbidden)
        result = await _collect(store, _config(workflows=False))

    assert result.unreadable == {"acme/a": "item enrichment: 1 failed"}
    assert result.exhausted is None


async def test_a_job_count_failure_makes_its_repository_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run the real job count cannot read reaches ``unreadable``."""
    from github_statistics.collect import enrich_jobs
    from github_statistics.store import Run

    store = _store()
    _Fakes(monkeypatch, _scope("a", "b"))
    monkeypatch.setattr(_run(), "enrich_jobs", enrich_jobs)
    store.upsert_runs(
        [
            Run(
                1,
                "WFR_1",
                "acme/b",
                ".github/workflows/ci.yaml",
                "push",
                "completed",
                "success",
                NOW,
                "alice",
                "User",
                1,
            )
        ]
    )

    with respx.mock:
        respx.post(f"{API}/graphql").mock(side_effect=_forbidden)
        result = await _collect(store)

    assert result.unreadable == {"acme/b": "job counts: 1 failed"}
