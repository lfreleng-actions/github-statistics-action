# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the job counts of workflow runs (design §6.4)."""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

GRAPHQL = "https://api.github.com/graphql"
REPO = "acme/a"


def _api() -> Any:
    """The collector, loaded when a test runs so a missing one fails that test."""
    return importlib.import_module("github_statistics.collect")


def _run(
    run_id: int,
    *,
    created: datetime | None = None,
    conclusion: str | None = "success",
    attempt: int = 1,
    repo: str = REPO,
) -> Any:
    """A stored run of ``repo``, its node id ``WFR_<run_id>``."""
    from github_statistics.store import Run

    return Run(
        id=run_id,
        node_id=f"WFR_{run_id}",
        repo=repo,
        workflow_path=".github/workflows/ci.yaml",
        event="push",
        status="completed" if conclusion else "in_progress",
        conclusion=conclusion,
        created_at=created or datetime(2026, 3, 1, tzinfo=UTC),
        actor="alice",
        actor_type="User",
        attempt=attempt,
    )


def _store(*runs: Any) -> Any:
    """An in-memory store holding ``runs``."""
    from github_statistics.store import Store

    store = Store(Path(":memory:"))
    store.upsert_runs(runs)
    return store


def _node(run_id: int, jobs: int) -> dict[str, Any]:
    """A ``WorkflowRun`` node whose latest attempt has ``jobs`` jobs."""
    return {
        "id": f"WFR_{run_id}",
        "databaseId": run_id,
        "checkSuite": {"checkRuns": {"totalCount": jobs}},
    }


def _serve(answer: Callable[[dict[str, Any]], httpx.Response]) -> respx.Route:
    """Mock GraphQL with ``answer(variables)``."""

    def respond(request: httpx.Request) -> httpx.Response:
        return answer(json.loads(request.content)["variables"])

    return respx.post(GRAPHQL).mock(side_effect=respond)


def _data(nodes: dict[str, Any], cost: int = 1) -> Callable[..., httpx.Response]:
    """Answer each batch with ``nodes[id]`` for every id it names."""

    def answer(variables: dict[str, Any]) -> httpx.Response:
        found = [nodes.get(node_id) for node_id in variables["ids"]]
        rate = {"cost": cost, "remaining": 4999}
        return httpx.Response(200, json={"data": {"rateLimit": rate, "nodes": found}})

    return answer


def _ids(route: respx.Route, call: int = 0) -> list[str]:
    """The node ids the ``call``-th query named."""
    return json.loads(route.calls[call].request.content)["variables"]["ids"]


async def _instant(_: float) -> None:
    return None


async def _enrich(
    store: Any, budget: Any = None, repos: tuple[str, ...] = (REPO,)
) -> Any:
    """Run ``enrich_jobs`` over ``repos`` with ``budget`` on its transport."""
    from github_statistics.client import Transport

    async with Transport(
        None, sleep=_instant, max_retries=0, budget=budget
    ) as transport:
        return await _api().enrich_jobs(transport, store, repos)


def _jobs(store: Any) -> dict[int, int | None]:
    """The stored job count of every run, by id."""
    rows = store.connection.execute("SELECT id, jobs FROM runs ORDER BY id")
    return dict(rows.fetchall())


@respx.mock
async def test_mixed_batch_stores_counts_newest_first() -> None:
    runs = [_run(n, created=datetime(2026, 3, n, tzinfo=UTC)) for n in (1, 2, 3, 4)]
    no_suite = {"id": "WFR_3", "databaseId": 3, "checkSuite": None}
    nodes = {"WFR_1": _node(1, 14), "WFR_2": None, "WFR_3": no_suite}
    nodes["WFR_4"] = _node(4, 3)
    route = _serve(_data(nodes))
    with _store(*runs) as store:
        result = await _enrich(store)

        assert route.call_count == 1
        assert _ids(route) == ["WFR_4", "WFR_3", "WFR_2", "WFR_1"]
        assert (result.runs, result.failed, result.remaining) == (4, (), ())
        assert _jobs(store) == {1: 14, 2: 0, 3: 0, 4: 3}
        assert store.runs_needing_jobs(10) == []


@respx.mock
async def test_runs_in_progress_wait_for_their_conclusion() -> None:
    route = _serve(_data({"WFR_1": _node(1, 2)}))
    with _store(_run(1), _run(2, conclusion=None)) as store:
        result = await _enrich(store)

        assert _ids(route) == ["WFR_1"]
        assert result.runs == 1 and result.remaining == ()
        assert _jobs(store) == {1: 2, 2: None}


@respx.mock
async def test_runs_out_of_scope_are_not_read() -> None:
    excluded = _run(2, created=datetime(2026, 3, 2, tzinfo=UTC), repo="acme/gone")
    route = _serve(_data({"WFR_1": _node(1, 4), "WFR_2": _node(2, 9)}))
    with _store(_run(1), excluded) as store:
        result = await _enrich(store)

        assert route.call_count == 1
        assert _ids(route) == ["WFR_1"]
        assert result.runs == 1 and result.remaining == ()
        assert _jobs(store) == {1: 4, 2: None}


@respx.mock
async def test_deleted_run_stores_zero_after_halving() -> None:
    def answer(variables: dict[str, Any]) -> httpx.Response:
        ids = variables["ids"]
        found = [None if i == "WFR_2" else _node(1, 5) for i in ids]
        errors = [
            {"type": "NOT_FOUND", "path": ["nodes", ids.index(i)]}
            for i in ids
            if i == "WFR_2"
        ]
        body: dict[str, Any] = {"data": {"nodes": found}}
        if errors:
            body["errors"] = errors
        return httpx.Response(200, json=body)

    route = _serve(answer)
    runs = [_run(n, created=datetime(2026, 3, n, tzinfo=UTC)) for n in (1, 2)]
    with _store(*runs) as store:
        result = await _enrich(store)

        assert route.call_count == 3
        assert (result.runs, result.failed) == (2, ())
        assert _jobs(store) == {1: 5, 2: 0}


@respx.mock
async def test_rerun_after_counting_is_counted_again() -> None:
    counts = iter((14, 15))

    def answer(variables: dict[str, Any]) -> httpx.Response:
        return _data({"WFR_1": _node(1, next(counts))})(variables)

    _serve(answer)
    with _store(_run(1)) as store:
        await _enrich(store)
        assert _jobs(store) == {1: 14}

        store.upsert_runs([_run(1, attempt=2)])
        assert _jobs(store) == {1: None}

        result = await _enrich(store)
        assert result.runs == 1
        assert _jobs(store) == {1: 15}


@respx.mock
async def test_spent_budget_stops_cleanly_newest_first() -> None:
    from github_statistics.client import Budget

    start = datetime(2026, 3, 1, tzinfo=UTC)
    runs = [_run(n, created=start + timedelta(minutes=n)) for n in range(1, 52)]
    nodes = {f"WFR_{n}": _node(n, 1) for n in range(1, 52)}
    budget = Budget(graphql_points=2)
    route = _serve(_data(nodes, cost=2))
    with _store(*runs) as store:
        result = await _enrich(store, budget)

        assert route.call_count == 1
        assert _ids(route) == [f"WFR_{n}" for n in range(51, 1, -1)]
        assert result.runs == 50 and result.remaining == ("WFR_1",)
        assert budget.graphql_spent == 2
        assert [r.id for r in store.runs_needing_jobs(10)] == [1]


@respx.mock
async def test_last_point_reads_one_run() -> None:
    from github_statistics.client import Budget

    runs = [_run(n, created=datetime(2026, 3, n, tzinfo=UTC)) for n in (1, 2)]
    route = _serve(_data({"WFR_1": _node(1, 1), "WFR_2": _node(2, 2)}))
    with _store(*runs) as store:
        result = await _enrich(store, Budget(graphql_points=1))

        assert _ids(route) == ["WFR_2"]
        assert result.runs == 1 and result.remaining == ("WFR_1",)
        assert _jobs(store) == {1: None, 2: 2}


@respx.mock
async def test_last_point_read_past_the_deadline_names_the_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A last-point read the clock outlasts names the time limit, not the point."""
    from github_statistics.client import Budget

    now = [0.0]
    runs = [_run(n, created=datetime(2026, 3, n, tzinfo=UTC)) for n in (1, 2)]
    answer = _data({"WFR_1": _node(1, 1), "WFR_2": _node(2, 2)})

    def read_then_time_out(variables: dict[str, Any]) -> httpx.Response:
        now[0] = 2.0
        return answer(variables)

    route = _serve(read_then_time_out)
    budget = Budget(graphql_points=1, deadline=1.0, clock=lambda: now[0])
    with _store(*runs) as store:
        with caplog.at_level(logging.INFO):
            result = await _enrich(store, budget)

        assert _ids(route) == ["WFR_2"]
        assert result.runs == 1 and result.remaining == ("WFR_1",)
        assert "point left" not in caplog.text
        [record] = [r for r in caplog.records if "time limit" in r.getMessage()]
        assert record.levelno == logging.INFO
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@respx.mock
async def test_budget_completing_no_run_names_the_setting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from github_statistics.client import Budget

    route = _serve(_data({"WFR_1": _node(1, 1)}))
    with _store(_run(1)) as store:
        with caplog.at_level(logging.WARNING):
            result = await _enrich(store, Budget(graphql_points=0))

        assert route.call_count == 0
        assert result.runs == 0 and result.remaining == ("WFR_1",)
        assert caplog.text.count("budget.graphql_points") == 1


@respx.mock
@pytest.mark.parametrize("rest_first", [False, True])
async def test_time_limit_counting_no_run_names_the_limit(
    caplog: pytest.LogCaptureFixture, rest_first: bool
) -> None:
    """The time limit is named even when the REST count ran out before it."""
    from github_statistics.client import Budget

    now = [0.0]
    budget = Budget(rest=0 if rest_first else 1, deadline=1.0, clock=lambda: now[0])
    assert budget.rest_exhausted is rest_first
    now[0] = 2.0
    route = _serve(_data({"WFR_1": _node(1, 1)}))
    with _store(_run(1)) as store:
        with caplog.at_level(logging.INFO):
            result = await _enrich(store, budget)

        assert route.call_count == 0
        assert result.remaining == ("WFR_1",)
        assert "budget" not in caplog.text
        counted = "time limit was reached before any run's jobs were counted; 1 wait"
        [record] = [r for r in caplog.records if counted in r.getMessage()]
        assert record.levelno == logging.INFO
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@respx.mock
@pytest.mark.parametrize("rest_first", [False, True])
async def test_time_limit_after_a_counted_batch_names_the_limit(
    caplog: pytest.LogCaptureFixture, rest_first: bool
) -> None:
    """A batch the clock stops after one was counted names the time limit."""
    from github_statistics.client import Budget

    now = [0.0]
    answer = _data({f"WFR_{i}": _node(i, 1) for i in range(1, 52)})

    def first_batch_then_time_out(variables: dict[str, Any]) -> httpx.Response:
        now[0] = 2.0
        return answer(variables)

    route = _serve(first_batch_then_time_out)
    budget = Budget(rest=0 if rest_first else 1, deadline=1.0, clock=lambda: now[0])
    assert budget.rest_exhausted is rest_first
    with _store(*(_run(i) for i in range(1, 52))) as store:
        with caplog.at_level(logging.INFO):
            result = await _enrich(store, budget)

        assert route.call_count == 1
        assert result.runs == 50 and len(result.remaining) == 1
        assert "budget" not in caplog.text
        [record] = [r for r in caplog.records if "time limit" in r.getMessage()]
        assert record.levelno == logging.INFO
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@respx.mock
@pytest.mark.parametrize("points", [None, 1])
async def test_forbidden_check_suite_stops_with_one_warning(
    caplog: pytest.LogCaptureFixture, points: int | None
) -> None:
    from github_statistics.client import Budget

    def answer(variables: dict[str, Any]) -> httpx.Response:
        ids = variables["ids"]
        found = [{"id": i, "databaseId": 1, "checkSuite": None} for i in ids]
        errors = [
            {
                "type": "FORBIDDEN",
                "path": ["nodes", n, "checkSuite"],
                "message": "Resource not accessible by integration",
            }
            for n in range(len(ids))
        ]
        return httpx.Response(200, json={"data": {"nodes": found}, "errors": errors})

    route = _serve(answer)
    runs = [_run(n, created=datetime(2026, 3, n, tzinfo=UTC)) for n in (1, 2)]
    with _store(*runs) as store:
        with caplog.at_level(logging.WARNING):
            budget = None if points is None else Budget(graphql_points=points)
            result = await _enrich(store, budget)

        assert route.call_count == 1
        assert result.runs == 0 and result.failed == ()
        assert result.remaining == ("WFR_2", "WFR_1")
        assert _jobs(store) == {1: None, 2: None}
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "checks: read" in warnings[0].getMessage()


@respx.mock
async def test_batch_is_halved_after_a_502() -> None:
    answer = _data({"WFR_1": _node(1, 4), "WFR_2": _node(2, 6)})

    def flaky(variables: dict[str, Any]) -> httpx.Response:
        if len(variables["ids"]) > 1:
            return httpx.Response(502)
        return answer(variables)

    route = _serve(flaky)
    runs = [_run(n, created=datetime(2026, 3, n, tzinfo=UTC)) for n in (1, 2)]
    with _store(*runs) as store:
        result = await _enrich(store)

        assert route.call_count == 3
        assert result.runs == 2
        assert _jobs(store) == {1: 4, 2: 6}


@respx.mock
async def test_id_failing_alone_is_left_for_the_next_run() -> None:
    def answer(variables: dict[str, Any]) -> httpx.Response:
        error = {"type": "SERVICE_UNAVAILABLE", "message": "no"}
        return httpx.Response(200, json={"data": {"nodes": [None]}, "errors": [error]})

    _serve(answer)
    with _store(_run(1)) as store:
        result = await _enrich(store)

        assert result.runs == 0 and result.failed == ("WFR_1",)
        assert _jobs(store) == {1: None}


@respx.mock
async def test_transport_error_ends_the_step_keeping_counts(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        importlib.import_module("github_statistics.collect.jobs"), "BATCH_SIZE", 1
    )
    answer = _data({"WFR_2": _node(2, 3)})

    def failing(request: httpx.Request) -> httpx.Response:
        variables = json.loads(request.content)["variables"]
        if variables["ids"] == ["WFR_1"]:
            raise httpx.ConnectError("down", request=request)
        return answer(variables)

    respx.post(GRAPHQL).mock(side_effect=failing)
    runs = [_run(n, created=datetime(2026, 3, n, tzinfo=UTC)) for n in (1, 2)]
    with _store(*runs) as store:
        with caplog.at_level(logging.WARNING):
            result = await _enrich(store)

        assert result.runs == 1 and result.remaining == ("WFR_1",)
        assert _jobs(store) == {1: None, 2: 3}
        assert "WFR_1" in caplog.text


def _day(day: int) -> datetime:
    """Noon on ``day`` March 2026."""
    return datetime(2026, 3, day, 12, tzinfo=UTC)


def _jobs_coverage(store: Any) -> list[tuple[int, int]]:
    """The jobs coverage of ``acme/a`` as (first, last) days of March."""
    return [(r.from_day.day, r.to_day.day) for r in store.coverage(REPO, "jobs")]


@respx.mock
async def test_counts_restore_jobs_coverage_within_runs_coverage() -> None:
    runs = [_run(n, created=_day(d)) for n, d in ((1, 1), (2, 2), (3, 2), (4, 5))]
    nodes = {f"WFR_{n}": _node(n, 2) for n in (1, 2, 3, 4)}
    _serve(_data(nodes))
    with _store(*runs) as store:
        store.add_coverage(REPO, "runs", _day(1).date(), _day(4).date())
        await _enrich(store)
        assert _jobs_coverage(store) == [(1, 4)]

        store.upsert_runs([_run(3, created=_day(2), attempt=2)])
        assert _jobs_coverage(store) == [(1, 1), (3, 4)]

        result = await _enrich(store)
        assert result.runs == 1
        assert _jobs_coverage(store) == [(1, 4)]


@respx.mock
async def test_partial_counts_cover_only_fully_counted_days() -> None:
    from github_statistics.client import Budget

    runs = [_run(n, created=_day(n)) for n in (1, 2)]
    runs.append(_run(3, created=_day(3), conclusion=None))
    _serve(_data({"WFR_1": _node(1, 1), "WFR_2": _node(2, 2)}))
    with _store(*runs) as store:
        store.add_coverage(REPO, "runs", _day(1).date(), _day(4).date())
        await _enrich(store, Budget(graphql_points=1))

        assert _jobs(store) == {1: None, 2: 2, 3: None}
        assert _jobs_coverage(store) == [(2, 2), (4, 4)]
