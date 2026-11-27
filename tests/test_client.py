# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the GitHub client: transport, paging, GraphQL and budget."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

API = "https://api.github.com"
TOKEN = "ghp_secret"


class Sleeper:
    """An injectable ``sleep`` that records each delay and returns at once."""

    def __init__(self) -> None:
        """Start with no delays."""
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        """Record ``delay``."""
        self.delays.append(delay)


def _transport(sleep: Sleeper, **extra: Any) -> Any:
    """A transport against the public API that sleeps through ``sleep``."""
    from github_statistics.client import Transport

    return Transport(TOKEN, sleep=sleep, **extra)


async def test_headers_and_anonymous() -> None:
    from github_statistics.client import Transport

    with respx.mock:
        route = respx.get(f"{API}/repos/acme/a").respond(200, json={})
        async with Transport(TOKEN) as transport:
            await transport.request("GET", "/repos/acme/a")
        async with Transport(None) as transport:
            await transport.request("GET", "/repos/acme/a")
    sent = route.calls[0].request.headers
    assert sent["Authorization"] == f"Bearer {TOKEN}"
    assert sent["Accept"] == "application/vnd.github+json"
    assert sent["X-GitHub-Api-Version"] == "2022-11-28"
    assert sent["User-Agent"] == "github-statistics"
    assert "Authorization" not in route.calls[1].request.headers
    assert TOKEN not in repr(transport)


def test_graphql_url() -> None:
    from github_statistics.client import graphql_url

    assert graphql_url("https://api.github.com") == "https://api.github.com/graphql"
    assert (
        graphql_url("https://ghe.example/api/v3/") == "https://ghe.example/api/graphql"
    )


async def test_transport_failure_backs_off_then_raises() -> None:
    from github_statistics.client import TransportError

    sleep = Sleeper()
    with respx.mock:
        route = respx.get(f"{API}/x").mock(side_effect=httpx.ConnectError("down"))
        async with _transport(sleep, max_retries=3) as transport:
            with pytest.raises(TransportError):
                await transport.request("GET", "/x")
    assert route.call_count == 4
    assert sleep.delays == [1.0, 2.0, 4.0]
    assert transport.stats.retries["transport"] == 3


async def test_transport_failure_recovers() -> None:
    sleep = Sleeper()
    with respx.mock:
        respx.get(f"{API}/x").mock(
            side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={"ok": 1})]
        )
        async with _transport(sleep) as transport:
            response = await transport.request("GET", "/x")
    assert response.json() == {"ok": 1}
    assert sleep.delays == [1.0]


async def test_wait_budget_stops_retries() -> None:
    from github_statistics.client import TransportError

    sleep = Sleeper()
    with respx.mock:
        route = respx.get(f"{API}/x").mock(side_effect=httpx.ConnectError("down"))
        async with _transport(sleep, max_retries=20) as transport:
            with pytest.raises(TransportError):
                await transport.request("GET", "/x")
    # 1 + 2 + 4 + 8 + 16 + 32 = 63; the next 64 would pass 120 s.
    assert sleep.delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]
    assert route.call_count == 7


async def test_401_fails_fast() -> None:
    from github_statistics.client import AuthError

    sleep = Sleeper()
    with respx.mock:
        route = respx.get(f"{API}/x").respond(401)
        async with _transport(sleep) as transport:
            with pytest.raises(AuthError) as caught:
                await transport.request("GET", "/x")
    assert route.call_count == 1
    assert sleep.delays == []
    assert TOKEN not in str(caught.value)


async def test_403_with_retry_after_is_a_rate_limit() -> None:
    sleep = Sleeper()
    with respx.mock:
        respx.get(f"{API}/x").mock(
            side_effect=[
                httpx.Response(403, headers={"Retry-After": "7"}),
                httpx.Response(200, json=[]),
            ]
        )
        async with _transport(sleep) as transport:
            response = await transport.request("GET", "/x")
    assert response.status_code == 200
    assert sleep.delays == [7.0]
    assert transport.stats.retries["rate_limit"] == 1


async def test_403_with_remaining_zero_is_a_rate_limit() -> None:
    sleep = Sleeper()
    with respx.mock:
        respx.get(f"{API}/x").mock(
            side_effect=[
                httpx.Response(403, headers={"x-ratelimit-remaining": "0"}),
                httpx.Response(200, json=[]),
            ]
        )
        async with _transport(sleep) as transport:
            response = await transport.request("GET", "/x")
    assert response.status_code == 200
    assert sleep.delays == [1.0]


async def test_403_without_rate_limit_headers_returns() -> None:
    sleep = Sleeper()
    with respx.mock:
        route = respx.get(f"{API}/x").respond(
            403, headers={"x-ratelimit-remaining": "42"}
        )
        async with _transport(sleep) as transport:
            response = await transport.request("GET", "/x")
    assert response.status_code == 403
    assert route.call_count == 1
    assert sleep.delays == []


async def test_429_without_headers_backs_off_exponentially() -> None:
    sleep = Sleeper()
    with respx.mock:
        respx.get(f"{API}/x").mock(
            side_effect=[
                httpx.Response(429),
                httpx.Response(429),
                httpx.Response(200, json=[]),
            ]
        )
        async with _transport(sleep) as transport:
            response = await transport.request("GET", "/x")
    assert response.status_code == 200
    assert sleep.delays == [1.0, 2.0]


async def test_malformed_retry_after_falls_back_to_backoff() -> None:
    sleep = Sleeper()
    with respx.mock:
        respx.get(f"{API}/x").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "soon"}),
                httpx.Response(200, json=[]),
            ]
        )
        async with _transport(sleep) as transport:
            await transport.request("GET", "/x")
    assert sleep.delays == [1.0]


def test_parse_retry_after() -> None:
    from github_statistics.client import parse_retry_after

    assert parse_retry_after("30") == 30.0
    assert parse_retry_after(" 1.5 ") == 1.5
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("-3") is None
    assert parse_retry_after("nan") is None


async def test_retry_after_beyond_wait_budget_returns_response() -> None:
    sleep = Sleeper()
    with respx.mock:
        route = respx.get(f"{API}/x").respond(429, headers={"Retry-After": "3600"})
        async with _transport(sleep) as transport:
            response = await transport.request("GET", "/x")
    assert response.status_code == 429
    assert route.call_count == 1
    assert sleep.delays == []


async def test_5xx_retries_then_returns_last_response() -> None:
    sleep = Sleeper()
    with respx.mock:
        route = respx.get(f"{API}/x").respond(503)
        async with _transport(sleep, max_retries=2) as transport:
            response = await transport.request("GET", "/x")
    assert response.status_code == 503
    assert route.call_count == 3
    assert sleep.delays == [1.0, 2.0]
    assert transport.stats.retries["server"] == 2
    assert transport.stats.requests == 3


async def test_foreign_host_refused() -> None:
    from github_statistics.client import ForeignHostError

    sleep = Sleeper()
    with respx.mock:
        async with _transport(sleep) as transport:
            for url in (
                "https://evil.example/x",
                "http://api.github.com/x",
                "https://api.github.com:8443/x",
            ):
                with pytest.raises(ForeignHostError):
                    await transport.request("GET", url)
        assert not respx.calls


async def test_get_paged_follows_next_across_three_pages() -> None:
    from github_statistics.client import get_paged

    sleep = Sleeper()
    with respx.mock:
        first = respx.get(
            f"{API}/repos/acme/a/issues", params__eq={"state": "all"}
        ).respond(
            200,
            json=[{"n": 1}],
            headers={
                "Link": f'<{API}/repos/acme/a/issues?state=all&page=2>; rel="next"'
            },
        )
        respx.get(
            f"{API}/repos/acme/a/issues", params__eq={"state": "all", "page": "2"}
        ).respond(
            200,
            json=[{"n": 2}],
            headers={
                "Link": f'<{API}/repos/acme/a/issues?state=all&page=3>; rel="next"'
            },
        )
        respx.get(
            f"{API}/repos/acme/a/issues", params__eq={"state": "all", "page": "3"}
        ).respond(200, json=[{"n": 3}])
        async with _transport(sleep) as transport:
            pages = [
                items
                async for items, _ in get_paged(
                    transport, "/repos/acme/a/issues", {"state": "all"}
                )
            ]
    assert pages == [[{"n": 1}], [{"n": 2}], [{"n": 3}]]
    assert first.call_count == 1


async def test_get_paged_items_key_and_foreign_next() -> None:
    from github_statistics.client import ForeignHostError, get_paged

    sleep = Sleeper()
    with respx.mock:
        respx.get(f"{API}/runs").respond(
            200,
            json={"total_count": 9, "workflow_runs": [{"id": 1}]},
            headers={"Link": '<https://evil.example/runs?page=2>; rel="next"'},
        )
        async with _transport(sleep) as transport:
            pages = get_paged(transport, "/runs", items_key="workflow_runs")
            items, _ = await anext(pages)
            assert items == [{"id": 1}]
            with pytest.raises(ForeignHostError):
                await anext(pages)


async def test_get_conditional_304_is_not_charged() -> None:
    from github_statistics.client import Budget, get_conditional

    sleep = Sleeper()
    budget = Budget()
    with respx.mock:
        route = respx.get(f"{API}/repos/acme/a").mock(
            side_effect=[
                httpx.Response(304),
                httpx.Response(200, json={"id": 1}, headers={"ETag": '"new"'}),
            ]
        )
        async with _transport(sleep, budget=budget) as transport:
            same = await get_conditional(transport, "/repos/acme/a", etag='"old"')
            fresh = await get_conditional(transport, "/repos/acme/a", etag='"old"')
    assert route.calls[0].request.headers["If-None-Match"] == '"old"'
    assert same.not_modified
    assert same.etag == '"old"'
    assert not fresh.not_modified
    assert fresh.body == {"id": 1}
    assert fresh.etag == '"new"'
    assert transport.stats.not_modified == 1
    assert budget.rest_spent == 1


async def test_graphql_error_payload_raises_and_charges_cost() -> None:
    from github_statistics.client import Budget, GraphQLError, graphql

    sleep = Sleeper()
    budget = Budget(graphql_points=10)
    with respx.mock:
        route = respx.post(f"{API}/graphql").respond(
            200,
            json={
                "data": {"rateLimit": {"cost": 3, "remaining": 4997}},
                "errors": [{"message": "Something went wrong"}],
            },
        )
        async with _transport(sleep, budget=budget) as transport:
            with pytest.raises(GraphQLError) as caught:
                await graphql(transport, "query { x }", {"a": 1}, cost_estimate=3)
    assert caught.value.status is None
    assert caught.value.errors == [{"message": "Something went wrong"}]
    assert budget.graphql_spent == 3
    assert budget.graphql_left == 7
    assert budget.rest_spent == 0
    assert transport.stats.graphql_remaining == 4997
    assert (
        route.calls[0].request.content == b'{"query":"query { x }","variables":{"a":1}}'
    )


async def test_graphql_query_is_retried_after_a_502() -> None:
    from github_statistics.client import GraphQLError, graphql

    sleep = Sleeper()
    with respx.mock:
        route = respx.post(f"{API}/graphql").mock(
            side_effect=[
                httpx.Response(502),
                httpx.Response(200, json={"data": {"x": 1}}),
                httpx.Response(502),
            ]
        )
        async with _transport(sleep) as transport:
            assert await graphql(transport, "query { x }") == {"x": 1}
        async with _transport(sleep, max_retries=0) as transport:
            with pytest.raises(GraphQLError) as caught:
                await graphql(transport, "query { x }")
    assert caught.value.status == 502
    assert route.call_count == 3
    assert sleep.delays == [1.0]


async def test_batched_halves_down_to_one_failing_id() -> None:
    from github_statistics.client import GraphQLError, batched

    calls: list[list[int]] = []

    async def fetch(chunk: list[int]) -> list[int]:
        calls.append(chunk)
        if 5 in chunk:
            raise GraphQLError("timeout", status=504 if len(chunk) > 2 else None)
        return chunk

    out = await batched(list(range(1, 11)), 8, fetch, lambda chunk: f"ids {chunk}")
    assert out.failed == [5]
    assert sorted(i for chunk in out.results for i in chunk) == [
        1,
        2,
        3,
        4,
        6,
        7,
        8,
        9,
        10,
    ]
    assert calls == [
        [1, 2, 3, 4, 5, 6, 7, 8],
        [1, 2, 3, 4],
        [5, 6, 7, 8],
        [5, 6],
        [5],
        [6],
        [7, 8],
        [9, 10],
    ]


async def test_batched_propagates_other_errors() -> None:
    from github_statistics.client import GraphQLError, batched

    async def fetch(chunk: list[int]) -> None:
        raise GraphQLError("forbidden", status=403)

    with pytest.raises(GraphQLError):
        await batched([1, 2], 2, fetch, str)


def test_budget_reserve_and_exhaustion() -> None:
    from github_statistics.client import Budget

    budget = Budget(rest=3, graphql_points=5, reserve=500)
    assert budget.rest_left == 3
    budget.observe(501)
    assert budget.rest_left == 1
    assert budget.try_spend_rest()
    budget.observe(500)
    assert budget.rest_left == 0
    assert budget.rest_exhausted
    assert not budget.try_spend_rest()
    budget.refund_rest()
    assert budget.rest_spent == 0
    assert budget.try_spend_graphql(4)
    assert not budget.try_spend_graphql(2)
    budget.settle_graphql(4, 5)
    assert budget.graphql_left == 0
    assert budget.graphql_exhausted


def test_budget_deadline_spends_like_an_empty_count_once_passed() -> None:
    """Spending works before the deadline and is refused from it on."""
    from github_statistics.client import Budget

    now = [0.0]
    budget = Budget(deadline=60.0, clock=lambda: now[0])
    assert budget.try_spend_rest()
    assert budget.try_spend_graphql(1)
    assert not budget.timed_out

    now[0] = 60.0

    assert budget.timed_out
    assert not budget.try_spend_rest()
    assert not budget.try_spend_graphql(1)
    assert budget.rest_exhausted
    assert budget.graphql_exhausted
    assert (budget.rest_spent, budget.graphql_spent) == (1, 1)
    assert not Budget(clock=lambda: 1e12).timed_out


def test_budget_records_whether_a_count_stopped_it_before_the_deadline() -> None:
    """A count found spent before the deadline is a stop; one after it is not."""
    from github_statistics.client import Budget

    now = [0.0]
    timed = Budget(deadline=60.0, clock=lambda: now[0])
    counted = Budget(rest=1, deadline=60.0, clock=lambda: now[0])
    assert counted.try_spend_rest()
    assert not counted.stopped_by_count
    assert counted.rest_exhausted
    assert counted.stopped_by_count

    now[0] = 60.0

    assert timed.timed_out
    timed.observe(0)
    timed.exhaust_graphql()
    assert timed.rest_exhausted
    assert timed.graphql_exhausted
    assert not timed.stopped_by_count
    assert counted.stopped_by_count


def test_budget_stop_reason_names_what_stopped_it_first() -> None:
    """The deadline is the reason only when no count stopped the run first."""
    from github_statistics.client import Budget

    now = [0.0]
    timed = Budget(deadline=60.0, clock=lambda: now[0])
    rest = Budget(rest=1, deadline=60.0, clock=lambda: now[0])
    points = Budget(graphql_points=1, deadline=60.0, clock=lambda: now[0])
    assert timed.stop_reason() is None
    assert rest.try_spend_rest() and not rest.try_spend_rest()
    assert not points.try_spend_graphql(2)

    now[0] = 60.0

    assert timed.stop_reason() == "time"
    assert rest.stop_reason() == "rest"
    assert points.stop_reason() == "graphql"


async def test_a_queued_query_holding_every_point_is_not_a_count_stop() -> None:
    """Points a query holds, then gives back unsent at the deadline, stop nothing."""
    import asyncio

    from github_statistics.client import Budget, BudgetExhaustedError, graphql

    now = [0.0]
    budget = Budget(graphql_points=2, deadline=60.0, clock=lambda: now[0])

    with respx.mock:
        route = respx.post(f"{API}/graphql").respond(200, json={"data": {}})
        async with _transport(Sleeper(), budget=budget, concurrency=1) as transport:
            async with transport._semaphore:
                queued = asyncio.ensure_future(
                    graphql(transport, "query { x }", cost_estimate=2)
                )
                await asyncio.sleep(0)
                assert budget.graphql_spent == 2
                now[0] = 60.0
            with pytest.raises(BudgetExhaustedError):
                await queued
    assert route.call_count == 0
    assert budget.graphql_spent == 0
    assert not budget.stopped_by_count


async def test_graphql_waiting_or_retrying_past_the_deadline_is_not_sent() -> None:
    """A query queued or retried past the deadline stays unsent."""
    import asyncio

    from github_statistics.client import Budget, BudgetExhaustedError, graphql

    now = [0.0]
    budget = Budget(graphql_points=10, deadline=60.0, clock=lambda: now[0])

    def fail_then_time_out(_request: httpx.Request) -> httpx.Response:
        now[0] = 60.0
        return httpx.Response(502)

    with respx.mock:
        route = respx.post(f"{API}/graphql").mock(side_effect=fail_then_time_out)
        async with _transport(Sleeper(), budget=budget, concurrency=1) as transport:
            with pytest.raises(BudgetExhaustedError):
                await graphql(transport, "query { x }", cost_estimate=1)
            assert route.call_count == 1
            now[0] = 0.0
            async with transport._semaphore:
                queued = asyncio.ensure_future(
                    graphql(transport, "query { x }", cost_estimate=2)
                )
                await asyncio.sleep(0)
                assert budget.graphql_spent == 3
                now[0] = 60.0
            with pytest.raises(BudgetExhaustedError):
                await queued
    assert route.call_count == 1
    assert budget.graphql_spent == 1


@pytest.mark.parametrize("retried", [False, True])
async def test_graphql_refused_past_the_deadline_names_the_time_limit(
    retried: bool,
) -> None:
    """A query, or its retry, refused at the deadline names it, not the budget."""
    from github_statistics.client import Budget, BudgetExhaustedError, graphql

    now = [0.0 if retried else 2.0]
    budget = Budget(rest=0, deadline=1.0, clock=lambda: now[0])
    assert budget.rest_exhausted

    def fail_late(_request: httpx.Request) -> httpx.Response:
        now[0] = 2.0
        return httpx.Response(502)

    with respx.mock:
        route = respx.post(f"{API}/graphql").mock(side_effect=fail_late)
        async with _transport(Sleeper(), budget=budget) as transport:
            with pytest.raises(
                BudgetExhaustedError,
                match="^time limit reached; not sending POST /graphql$",
            ):
                await graphql(transport, "query { x }", cost_estimate=1)
    assert route.call_count == int(retried)


async def test_rest_refused_past_the_deadline_names_the_time_limit() -> None:
    """A REST request refused at the deadline says so, not that the budget ran out."""
    from github_statistics.client import Budget, BudgetExhaustedError

    budget = Budget(deadline=1.0, clock=lambda: 2.0)
    with respx.mock:
        route = respx.get(f"{API}/x").respond(200, json=[])
        async with _transport(Sleeper(), budget=budget) as transport:
            with pytest.raises(
                BudgetExhaustedError, match="^time limit reached; not sending GET /x$"
            ):
                await transport.request("GET", "/x")
    assert route.call_count == 0


async def test_rest_retry_refused_past_the_deadline_names_the_time_limit() -> None:
    """A retry refused at the deadline names it, with REST requests still left."""
    from github_statistics.client import Budget, BudgetExhaustedError

    now = [0.0]
    budget = Budget(rest=10, deadline=1.0, clock=lambda: now[0])

    def fail_late(request: httpx.Request) -> httpx.Response:
        now[0] = 2.0
        return httpx.Response(502)

    with respx.mock:
        route = respx.get(f"{API}/x").mock(side_effect=fail_late)
        async with _transport(Sleeper(), budget=budget) as transport:
            with pytest.raises(
                BudgetExhaustedError, match="^time limit reached; not retrying GET /x$"
            ):
                await transport.request("GET", "/x")
    assert route.call_count == 1
    assert budget.rest_spent == 1


def test_budget_stop_reason_of_one_resource_names_the_time_limit() -> None:
    """A resource the clock stopped after the other's count ran out names it."""
    from github_statistics.client import Budget

    now = [0.0]
    rest = Budget(rest=0, deadline=60.0, clock=lambda: now[0])
    points = Budget(deadline=60.0, clock=lambda: now[0])
    assert rest.rest_exhausted
    points.exhaust_graphql()
    assert rest.stop_reason("graphql") is None

    now[0] = 60.0

    assert rest.stop_reason() == "rest"
    assert (rest.stop_reason("rest"), rest.stop_reason("graphql")) == ("rest", "time")
    assert points.stop_reason() == "graphql"
    assert (points.stop_reason("graphql"), points.stop_reason("rest")) == (
        "graphql",
        "time",
    )


async def test_budget_seeded_free_and_charged_per_request() -> None:
    from github_statistics.client import Budget

    sleep = Sleeper()
    budget = Budget(rest=3000, reserve=500)
    with respx.mock:
        respx.get(f"{API}/rate_limit").respond(
            200,
            json={"resources": {"core": {"remaining": 700}}},
            headers={"x-ratelimit-remaining": "700"},
        )
        respx.get(f"{API}/x").respond(
            200,
            json=[],
            headers={"x-ratelimit-remaining": "699", "x-ratelimit-reset": "1700000000"},
        )
        async with _transport(sleep, budget=budget) as transport:
            await transport.seed_budget()
            assert budget.rest_spent == 0
            assert budget.rest_left == 200
            await transport.request("GET", "/x")
    assert budget.rest_spent == 1
    assert budget.rest_left == 199
    assert transport.stats.rate_limit_remaining == 699
    assert transport.stats.rate_limit_reset == 1700000000
    assert transport.stats.bytes_received > 0


def test_budget_observations_are_monotonic_per_window() -> None:
    from github_statistics.client import Budget

    budget = Budget(rest=3000, reserve=500)
    budget.observe(600, reset=100)
    budget.observe(700, reset=100)
    assert budget.rest_left == 100
    budget.observe(550, reset=90)
    assert budget.rest_left == 100
    budget.observe(900, reset=200)
    assert budget.rest_left == 400


async def test_budget_reserved_before_each_concurrent_attempt() -> None:
    import asyncio

    from github_statistics.client import Budget, BudgetExhaustedError

    sleep = Sleeper()
    budget = Budget(rest=1)
    budget_retry = Budget(rest=2)
    with respx.mock:
        once = respx.get(f"{API}/x").respond(200, json=[])
        failing = respx.get(f"{API}/down").respond(503)
        async with _transport(sleep, budget=budget) as transport:
            results = await asyncio.gather(
                transport.request("GET", "/x"),
                transport.request("GET", "/x"),
                return_exceptions=True,
            )
        async with _transport(sleep, budget=budget_retry) as transport:
            with pytest.raises(BudgetExhaustedError):
                await transport.request("GET", "/down")
    assert sum(isinstance(r, BudgetExhaustedError) for r in results) == 1
    assert once.call_count == 1
    assert budget.rest_left == 0
    assert failing.call_count == 2


async def test_budget_keeps_charge_for_sent_failures_only() -> None:
    from github_statistics.client import Budget, TransportError

    sleep = Sleeper()
    budget = Budget()
    with respx.mock:
        respx.get(f"{API}/refused").mock(side_effect=httpx.ConnectError("down"))
        respx.get(f"{API}/slow").mock(side_effect=httpx.ReadTimeout("slow"))
        async with _transport(sleep, budget=budget, max_retries=1) as transport:
            with pytest.raises(TransportError):
                await transport.request("GET", "/refused")
            assert budget.rest_spent == 0
            with pytest.raises(TransportError):
                await transport.request("GET", "/slow")
    assert budget.rest_spent == 2


async def test_redirect_followed_only_within_origin() -> None:
    from github_statistics.client import ForeignHostError

    sleep = Sleeper()
    with respx.mock:
        respx.get(f"{API}/repos/acme/old").respond(
            301, headers={"Location": f"{API}/repositories/7"}
        )
        moved = respx.get(f"{API}/repositories/7").respond(200, json={"id": 7})
        respx.get(f"{API}/repos/acme/away").respond(
            301, headers={"Location": "https://evil.example/steal"}
        )
        evil = respx.get("https://evil.example/steal").respond(200)
        async with _transport(sleep) as transport:
            response = await transport.request("GET", "/repos/acme/old")
            with pytest.raises(ForeignHostError):
                await transport.request("GET", "/repos/acme/away")
    assert response.json() == {"id": 7}
    assert moved.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert not evil.called


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_redirect_statuses_followed_for_get(status: int) -> None:
    sleep = Sleeper()
    with respx.mock:
        respx.get(f"{API}/old").respond(status, headers={"Location": "/new"})
        moved = respx.get(f"{API}/new").respond(200)
        async with _transport(sleep) as transport:
            response = await transport.request("GET", "/old")
    assert response.status_code == 200
    assert moved.called


async def test_seed_budget_raises_on_failure_other_than_404() -> None:
    from github_statistics.client import Budget, ClientError

    sleep = Sleeper()
    budget = Budget()
    with respx.mock:
        respx.get(f"{API}/rate_limit").mock(
            side_effect=[
                httpx.Response(404),
                httpx.Response(503),
                httpx.Response(200, json={"resources": {}}),
            ]
        )
        async with _transport(sleep, budget=budget, max_retries=0) as transport:
            await transport.seed_budget()
            assert budget.rest_left == 3000
            with pytest.raises(httpx.HTTPStatusError):
                await transport.seed_budget()
            with pytest.raises(ClientError):
                await transport.seed_budget()


async def test_304_observes_quota_and_is_free_only_when_authenticated() -> None:
    from github_statistics.client import Budget, Transport

    sleep = Sleeper()
    budget = Budget(reserve=500)
    anonymous = Budget()
    with respx.mock:
        respx.get(f"{API}/rate_limit").respond(
            200, json={"resources": {"core": {"remaining": 900, "reset": 9}}}
        )
        respx.get(f"{API}/repos/acme/a").respond(
            304, headers={"x-ratelimit-remaining": "800", "x-ratelimit-reset": "9"}
        )
        async with _transport(sleep, budget=budget) as transport:
            await transport.seed_budget()
            await transport.request("GET", "/repos/acme/a")
        async with Transport(None, budget=anonymous) as transport:
            await transport.request("GET", "/repos/acme/a")
    assert budget.rest_spent == 0
    assert budget.rest_left == 300
    assert anonymous.rest_spent == 1


async def test_graphql_request_wide_errors_are_not_split() -> None:
    from github_statistics.client import GraphQLError, batched, graphql

    sleep = Sleeper()
    with respx.mock:
        respx.post(f"{API}/graphql").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={"errors": [{"type": "RATE_LIMITED", "message": "slow down"}]},
                ),
                httpx.Response(200, json={"errors": [{"message": "bad field"}]}),
                httpx.Response(
                    200,
                    json={
                        "errors": [
                            {"type": "MAX_NODE_LIMIT_EXCEEDED", "message": "big"}
                        ]
                    },
                ),
                httpx.Response(200, json={"data": None, "errors": [{"message": "x"}]}),
            ]
        )
        scopes = []
        for _ in range(4):
            async with _transport(sleep) as transport:
                with pytest.raises(GraphQLError) as caught:
                    await graphql(transport, "query { x }")
            scopes.append(caught.value.request_wide)
    assert scopes == [True, True, False, False]

    calls: list[list[int]] = []

    async def fetch(chunk: list[int]) -> None:
        calls.append(chunk)
        raise GraphQLError("rate limited", request_wide=True)

    with pytest.raises(GraphQLError):
        await batched(list(range(50)), 50, fetch, str)
    assert len(calls) == 1


async def test_anonymous_reads_get_their_own_default_reserve() -> None:
    from github_statistics.client import Budget, Transport

    budget = Budget()
    explicit = Budget(reserve=500)
    with respx.mock:
        respx.get(f"{API}/rate_limit").respond(
            200, json={"resources": {"core": {"remaining": 60, "reset": 9}}}
        )
        async with (
            Transport(None, budget=budget) as anonymous,
            Transport(None, budget=explicit) as strict,
        ):
            await anonymous.seed_budget()
            await strict.seed_budget()
            assert anonymous.rest_left == 50
            assert strict.rest_left == 0


async def test_graphql_points_held_before_concurrent_queries() -> None:
    import asyncio

    from github_statistics.client import Budget, BudgetExhaustedError, graphql

    sleep = Sleeper()
    budget = Budget(graphql_points=1)
    with respx.mock:
        route = respx.post(f"{API}/graphql").respond(
            200, json={"data": {"rateLimit": {"cost": 1, "remaining": 9}}}
        )
        async with _transport(sleep, budget=budget) as transport:
            results = await asyncio.gather(
                graphql(transport, "query { x }", cost_estimate=1),
                graphql(transport, "query { x }", cost_estimate=1),
                return_exceptions=True,
            )
    assert sum(isinstance(r, BudgetExhaustedError) for r in results) == 1
    assert route.call_count == 1
    assert budget.graphql_spent == 1
    assert budget.graphql_exhausted


async def test_batched_returns_results_and_remaining_ids_on_exhaustion() -> None:
    from github_statistics.client import BudgetExhaustedError, batched

    async def fetch(chunk: list[int]) -> list[int]:
        if 5 in chunk:
            raise BudgetExhaustedError("out")
        return chunk

    out = await batched(list(range(1, 11)), 2, fetch, str)
    assert out.results == [[1, 2], [3, 4]]
    assert out.remaining == [5, 6, 7, 8, 9, 10]
    assert out.failed == []


async def test_get_paged_resolves_a_relative_next_link() -> None:
    from github_statistics.client import get_paged

    sleep = Sleeper()
    with respx.mock:
        respx.get(f"{API}/repos/acme/a/issues", params__eq={}).respond(
            200, json=[{"n": 1}], headers={"Link": '<?page=2>; rel="next"'}
        )
        second = respx.get(
            f"{API}/repos/acme/a/issues", params__eq={"page": "2"}
        ).respond(200, json=[{"n": 2}])
        async with _transport(sleep) as transport:
            pages = [
                items async for items, _ in get_paged(transport, "/repos/acme/a/issues")
            ]
    assert pages == [[{"n": 1}], [{"n": 2}]]
    assert second.call_count == 1


async def test_retry_limits_span_redirect_hops() -> None:
    sleep = Sleeper()
    with respx.mock:
        respx.get(f"{API}/repos/acme/old").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(301, headers={"Location": f"{API}/repositories/7"}),
            ]
        )
        respx.get(f"{API}/repositories/7").mock(
            side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200)]
        )
        async with _transport(sleep, max_wait=3.0) as transport:
            response = await transport.request("GET", "/repos/acme/old")
    assert response.status_code == 503
    assert sleep.delays == [1.0, 2.0]


async def test_exhausted_budget_stops_before_backing_off() -> None:
    from github_statistics.client import Budget, BudgetExhaustedError

    sleep = Sleeper()
    budget = Budget(reserve=500)
    with respx.mock:
        respx.get(f"{API}/rate_limit").respond(
            200, json={"resources": {"core": {"remaining": 900, "reset": 9}}}
        )
        route = respx.get(f"{API}/x").respond(
            429,
            headers={
                "Retry-After": "120",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "9",
            },
        )
        async with _transport(sleep, budget=budget) as transport:
            await transport.seed_budget()
            with pytest.raises(BudgetExhaustedError):
                await transport.request("GET", "/x")
    assert route.call_count == 1
    assert sleep.delays == []


async def test_budgeted_graphql_needs_an_estimate() -> None:
    from github_statistics.client import Budget, graphql

    sleep = Sleeper()
    with respx.mock:
        route = respx.post(f"{API}/graphql").respond(200, json={"data": {}})
        async with _transport(sleep, budget=Budget()) as transport:
            with pytest.raises(ValueError):
                await graphql(transport, "query { x }")
    assert not route.called


async def test_final_rate_limit_raises_when_the_budget_is_exhausted() -> None:
    from github_statistics.client import Budget, BudgetExhaustedError

    sleep = Sleeper()
    budget = Budget(reserve=500)
    with respx.mock:
        respx.get(f"{API}/rate_limit").respond(
            200, json={"resources": {"core": {"remaining": 900, "reset": 9}}}
        )
        long = respx.get(f"{API}/x").respond(
            429,
            headers={
                "Retry-After": "3600",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "9",
            },
        )
        forbidden = respx.get(f"{API}/y").respond(
            403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9"}
        )
        async with _transport(sleep, budget=budget) as transport:
            await transport.seed_budget()
            with pytest.raises(BudgetExhaustedError):
                await transport.request("GET", "/x")
        async with _transport(sleep, budget=Budget(), max_retries=0) as transport:
            with pytest.raises(BudgetExhaustedError):
                await transport.request("GET", "/y")
    assert long.call_count == 1
    assert sleep.delays == []
    assert forbidden.call_count == 1


async def test_graphql_points_taken_once_per_query() -> None:
    from github_statistics.client import Budget, BudgetExhaustedError, graphql

    sleep = Sleeper()
    budget = Budget(graphql_points=5)
    short = Budget(graphql_points=1)
    with respx.mock:
        route = respx.post(f"{API}/graphql").mock(
            side_effect=[
                httpx.ReadTimeout("slow"),
                httpx.Response(200, json={"data": {"rateLimit": {"cost": 1}}}),
            ]
        )
        async with _transport(sleep, budget=budget) as transport:
            await graphql(transport, "query { x }", cost_estimate=2)
        async with _transport(sleep, budget=short) as transport:
            with pytest.raises(BudgetExhaustedError):
                await graphql(transport, "query { x }", cost_estimate=2)
    assert budget.graphql_spent == 1
    assert short.graphql_spent == 0
    assert route.call_count == 2


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
)
async def test_request_refuses_every_method_but_get(method: str) -> None:
    from github_statistics.client import Budget

    sleep = Sleeper()
    budget = Budget()
    with respx.mock:
        route = respx.route(url__regex=rf"{API}/.*").respond(200, json={})
        async with _transport(sleep, budget=budget) as transport:
            with pytest.raises(ValueError):
                await transport.request(method, "/repos/acme/a")
            with pytest.raises(ValueError):
                await transport.request(method.lower(), "/graphql")
    assert not route.called
    assert budget.rest_spent == 0


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_graphql_redirect_is_an_error(status: int) -> None:
    from github_statistics.client import Budget, GraphQLError, graphql

    sleep = Sleeper()
    budget = Budget(graphql_points=5)
    with respx.mock:
        respx.post(f"{API}/graphql").respond(
            status, headers={"Location": f"{API}/graphql/"}
        )
        moved = respx.route(url=f"{API}/graphql/").respond(200, json={"data": {}})
        async with _transport(sleep, budget=budget) as transport:
            with pytest.raises(GraphQLError) as caught:
                await graphql(transport, "query { x }", cost_estimate=2)
    assert caught.value.status == status
    assert not moved.called
    assert budget.graphql_spent == 2


async def test_graphql_rate_limit_stops_the_budget() -> None:
    from github_statistics.client import Budget, BudgetExhaustedError, batched, graphql

    sleep = Sleeper()
    payload = Budget(graphql_points=50)
    status = Budget(graphql_points=50)
    with respx.mock:
        route = respx.post(f"{API}/graphql").mock(
            side_effect=[
                httpx.Response(200, json={"data": {"rateLimit": {"cost": 1}}}),
                httpx.Response(
                    200,
                    json={"errors": [{"type": "RATE_LIMITED", "message": "slow down"}]},
                ),
                httpx.Response(429, headers={"Retry-After": "3600"}),
            ]
        )
        async with _transport(sleep, budget=payload) as transport:

            async def fetch(chunk: list[int]) -> list[int]:
                await graphql(transport, "query { x }", cost_estimate=1)
                return chunk

            out = await batched([1, 2, 3], 1, fetch, str)
        async with _transport(sleep, budget=status) as transport:
            with pytest.raises(BudgetExhaustedError):
                await graphql(transport, "query { x }", cost_estimate=1)
    assert out.results == [[1]]
    assert out.remaining == [2, 3]
    assert payload.graphql_exhausted
    assert status.graphql_exhausted
    assert route.call_count == 3
    assert sleep.delays == []


async def test_graphql_secondary_rate_limit_403_stops_the_budget() -> None:
    from github_statistics.client import (
        Budget,
        BudgetExhaustedError,
        GraphQLError,
        batched,
        graphql,
    )

    sleep = Sleeper()
    budget = Budget(graphql_points=50)
    secondary = {
        "message": "You have exceeded a secondary rate limit. Please wait.",
        "documentation_url": "https://docs.github.com/graphql#secondary-rate-limits",
    }
    with respx.mock:
        route = respx.post(f"{API}/graphql").mock(
            side_effect=[
                httpx.Response(200, json={"data": {"rateLimit": {"cost": 1}}}),
                httpx.Response(403, json=secondary),
                httpx.Response(403, json={"message": "Resource not accessible"}),
                httpx.Response(403, json=secondary),
            ]
        )
        async with _transport(sleep, budget=budget, max_retries=0) as transport:

            async def fetch(chunk: list[int]) -> list[int]:
                await graphql(transport, "query { x }", cost_estimate=1)
                return chunk

            out = await batched([1, 2, 3], 1, fetch, str)
            assert budget.graphql_exhausted
            with pytest.raises(BudgetExhaustedError):
                await graphql(transport, "query { x }", cost_estimate=1)
        async with _transport(sleep, max_retries=0) as transport:
            with pytest.raises(GraphQLError) as denied:
                await graphql(transport, "query { x }")
            with pytest.raises(GraphQLError) as limited:
                await graphql(transport, "query { x }")
    assert out.results == [[1]]
    assert out.remaining == [2, 3]
    assert route.call_count == 4
    assert sleep.delays == []
    assert denied.value.status == 403 and not denied.value.request_wide
    assert limited.value.status == 403 and limited.value.request_wide


async def test_seed_budget_falls_back_to_a_core_header_only() -> None:
    from github_statistics.client import Budget, ClientError

    sleep = Sleeper()
    search = Budget()
    core = Budget()
    with respx.mock:
        respx.get(f"{API}/rate_limit").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={"resources": {}},
                    headers={
                        "x-ratelimit-resource": "search",
                        "x-ratelimit-remaining": "20",
                    },
                ),
                httpx.Response(
                    200,
                    json={"resources": {}},
                    headers={"x-ratelimit-remaining": "900"},
                ),
            ]
        )
        async with _transport(sleep, budget=search) as transport:
            with pytest.raises(ClientError):
                await transport.seed_budget()
        async with _transport(sleep, budget=core) as transport:
            await transport.seed_budget()
    assert search.remaining is None
    assert core.remaining == 900


def test_graphql_rate_limit_outlasts_settlement() -> None:
    from github_statistics.client import Budget

    budget = Budget(graphql_points=10)
    assert budget.try_spend_graphql(1)
    assert budget.try_spend_graphql(5)
    budget.exhaust_graphql()
    budget.settle_graphql(5, 1)
    assert budget.graphql_exhausted
    assert budget.graphql_left == 0
    assert not budget.try_spend_graphql(1)


async def test_graphql_estimate_refunded_only_when_nothing_was_sent() -> None:
    import asyncio

    from github_statistics.client import Budget, TransportError, graphql

    sleep = Sleeper()
    unsent = Budget(graphql_points=5)
    sent = Budget(graphql_points=5)
    queued = Budget(graphql_points=5)
    with respx.mock:
        respx.post(f"{API}/graphql").mock(
            side_effect=[httpx.ConnectError("refused"), httpx.ReadTimeout("slow")]
        )
        async with _transport(sleep, budget=unsent, max_retries=0) as transport:
            with pytest.raises(TransportError):
                await graphql(transport, "query { x }", cost_estimate=5)
        async with _transport(sleep, budget=sent, max_retries=0) as transport:
            with pytest.raises(TransportError):
                await graphql(transport, "query { x }", cost_estimate=5)
        async with _transport(sleep, budget=queued, concurrency=1) as transport:
            async with transport._semaphore:
                task = asyncio.ensure_future(
                    graphql(transport, "query { x }", cost_estimate=5)
                )
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
    assert unsent.graphql_left == 5
    assert sent.graphql_left == 0
    assert queued.graphql_left == 5


def test_transport_requires_positive_concurrency() -> None:
    from github_statistics.client import Transport

    with pytest.raises(ValueError):
        Transport(TOKEN, concurrency=0)


async def test_401_stops_queued_and_later_requests() -> None:
    import asyncio

    from github_statistics.client import AuthError

    sleep = Sleeper()
    with respx.mock:
        route = respx.get(f"{API}/x").respond(401)
        async with _transport(sleep, concurrency=1) as transport:
            results = await asyncio.gather(
                *(transport.request("GET", "/x") for _ in range(3)),
                return_exceptions=True,
            )
            with pytest.raises(AuthError):
                await transport.request("GET", "/x")
    assert all(isinstance(result, AuthError) for result in results)
    assert route.call_count == 1


async def test_graphql_stop_checked_before_a_queued_query_is_sent() -> None:
    import asyncio

    from github_statistics.client import Budget, BudgetExhaustedError, graphql

    sleep = Sleeper()
    budget = Budget(graphql_points=10)
    with respx.mock:
        route = respx.post(f"{API}/graphql").respond(
            429, headers={"Retry-After": "3600"}
        )
        async with _transport(sleep, budget=budget, concurrency=1) as transport:
            async with transport._semaphore:
                first = asyncio.ensure_future(
                    graphql(transport, "query { x }", cost_estimate=1)
                )
                second = asyncio.ensure_future(
                    graphql(transport, "query { x }", cost_estimate=1)
                )
                await asyncio.sleep(0)
            results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, BudgetExhaustedError) for result in results)
    assert budget.graphql_stopped
    assert route.call_count == 1


async def test_401_raised_before_graphql_points_are_taken() -> None:
    from github_statistics.client import AuthError, Budget, graphql

    sleep = Sleeper()
    budget = Budget(graphql_points=1)
    with respx.mock:
        route = respx.post(f"{API}/graphql").respond(401)
        async with _transport(sleep, budget=budget) as transport:
            for _ in range(2):
                with pytest.raises(AuthError):
                    await graphql(transport, "query { x }", cost_estimate=1)
    assert budget.graphql_left == 0
    assert route.call_count == 1


async def test_budgeted_graphql_rate_limit_retries_before_stopping() -> None:
    from github_statistics.client import Budget, BudgetExhaustedError, graphql

    sleep = Sleeper()
    budget = Budget(graphql_points=10)
    with respx.mock:
        route = respx.post(f"{API}/graphql").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "60"}),
                httpx.Response(200, json={"data": {"x": 1}}),
                httpx.Response(429, headers={"Retry-After": "60"}),
                httpx.Response(403, headers={"Retry-After": "60"}),
            ]
        )
        async with _transport(sleep, budget=budget, max_retries=1) as transport:
            data = await graphql(transport, "query { x }", cost_estimate=1)
            assert not budget.graphql_stopped
            with pytest.raises(BudgetExhaustedError):
                await graphql(transport, "query { x }", cost_estimate=1)
    assert data["x"] == 1
    assert budget.graphql_stopped
    assert sleep.delays == [60.0, 60.0]
    assert route.call_count == 4


async def test_token_refused_over_plain_http() -> None:
    from github_statistics.client import Transport

    with pytest.raises(ValueError, match="non-HTTPS"):
        Transport(TOKEN, api_url="http://ghe.example/api/v3")
    with respx.mock:
        route = respx.get("http://ghe.example/api/v3/x").respond(200)
        async with Transport(None, api_url="http://ghe.example/api/v3") as transport:
            await transport.request("GET", "/x")
        async with Transport(
            TOKEN, api_url="http://ghe.example/api/v3", insecure=True
        ) as transport:
            await transport.request("GET", "/x")
    assert "Authorization" not in route.calls[0].request.headers
    assert route.calls[1].request.headers["Authorization"] == f"Bearer {TOKEN}"


async def test_batched_stops_after_too_many_timeouts() -> None:
    from github_statistics.client import GraphQLError, batched

    calls: list[list[int]] = []

    async def fetch(chunk: list[int]) -> list[int]:
        calls.append(chunk)
        if 2 in chunk or 7 in chunk:
            raise GraphQLError("timed out", status=504)
        return chunk

    out = await batched(list(range(1, 9)), 4, fetch, str, max_timeouts=4)
    assert calls == [[1, 2, 3, 4], [1, 2], [1], [2], [3, 4], [5, 6, 7, 8]]
    assert out.results == [[1], [3, 4]]
    assert out.failed == [2]
    assert out.remaining == [5, 6, 7, 8]


async def test_final_search_rate_limit_raises_budget_exhausted() -> None:
    from github_statistics.client import Budget, BudgetExhaustedError

    sleep = Sleeper()
    budget = Budget(reserve=500)
    with respx.mock:
        respx.get(f"{API}/rate_limit").respond(
            200, json={"resources": {"core": {"remaining": 900, "reset": 9}}}
        )
        route = respx.get(f"{API}/search/issues").respond(
            403,
            headers={
                "x-ratelimit-resource": "search",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "9",
            },
        )
        async with _transport(sleep, budget=budget, max_retries=0) as transport:
            await transport.seed_budget()
            with pytest.raises(BudgetExhaustedError):
                await transport.request("GET", "/search/issues")
    assert route.call_count == 1
    assert budget.remaining == 900


async def test_graphql_secondary_rate_limit_message_is_a_rate_limit() -> None:
    from github_statistics.client import Budget, GraphQLError, batched, graphql

    sleep = Sleeper()
    budget = Budget(graphql_points=50)
    limited = {"errors": [{"message": "You have exceeded a secondary rate limit."}]}
    with respx.mock:
        route = respx.post(f"{API}/graphql").mock(
            side_effect=[
                httpx.Response(200, json={"data": None, **limited}),
                httpx.Response(200, json={"data": None, **limited}),
                httpx.Response(403, json=limited),
            ]
        )
        async with _transport(sleep, budget=budget) as transport:

            async def fetch(chunk: list[int]) -> list[int]:
                await graphql(transport, "query { x }", cost_estimate=1)
                return chunk

            out = await batched([1, 2, 3], 3, fetch, str)
        async with _transport(sleep) as transport:
            with pytest.raises(GraphQLError) as payload:
                await graphql(transport, "query { x }")
        async with _transport(sleep, max_retries=0) as transport:
            with pytest.raises(GraphQLError) as forbidden:
                await graphql(transport, "query { x }")
    assert budget.graphql_stopped
    assert out.remaining == [1, 2, 3]
    assert not out.failed
    assert payload.value.request_wide
    assert forbidden.value.status == 403 and forbidden.value.request_wide
    assert route.call_count == 3


async def test_params_merge_into_the_url_query() -> None:
    from github_statistics.client import get_paged

    sleep = Sleeper()
    with respx.mock:
        route = respx.get(path="/repos/o/r/issues").respond(200, json=[])
        other = respx.get(path="/x").respond(200)
        async with _transport(sleep) as transport:
            async for _ in get_paged(
                transport, "/repos/o/r/issues?state=all", {"per_page": 100}
            ):
                pass
            await transport.request("GET", "/x?a=1&b=2", params={"b": 3})
    sent = route.calls[0].request.url.params
    assert sent["state"] == "all" and sent["per_page"] == "100"
    assert dict(other.calls[0].request.url.params) == {"a": "1", "b": "3"}


async def test_unbudgeted_graphql_rate_limit_statuses_are_request_wide() -> None:
    from github_statistics.client import GraphQLError, graphql

    sleep = Sleeper()
    with respx.mock:
        route = respx.post(f"{API}/graphql").mock(
            side_effect=[
                httpx.Response(429),
                httpx.Response(403, headers={"Retry-After": "1"}),
                httpx.Response(403, headers={"x-ratelimit-remaining": "0"}),
                httpx.Response(403, json={"message": "Resource not accessible"}),
            ]
        )
        caught = []
        for _ in range(4):
            async with _transport(sleep, max_retries=0) as transport:
                with pytest.raises(GraphQLError) as failure:
                    await graphql(transport, "query { x }")
            caught.append(failure.value)
    assert [error.status for error in caught] == [429, 403, 403, 403]
    assert [error.request_wide for error in caught] == [True, True, True, False]
    assert route.call_count == 4


@pytest.mark.parametrize(
    "document",
    ["mutation { addStar { x } }", "subscription S { event { id } }"],
)
async def test_graphql_refuses_mutations_and_subscriptions(document: str) -> None:
    from github_statistics.client import Budget, graphql

    sleep = Sleeper()
    budget = Budget(graphql_points=5)
    with respx.mock:
        route = respx.post(f"{API}/graphql").respond(200, json={"data": {}})
        async with _transport(sleep, budget=budget) as transport:
            with pytest.raises(ValueError):
                await graphql(transport, document, cost_estimate=1)
    assert not route.called
    assert budget.graphql_spent == 0


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("# star\nmutation { addStar { x } }", "mutation"),
        ("query A { a } subscription B { b }", "subscription"),
        (",mutation { a }", "mutation"),
        ("\ufeffmutation { a }", "mutation"),
        ('query { a(x: "#") } mutation { b }', "mutation"),
        ('{ a(x: """ { \\""" """) } mutation { b }', "mutation"),
        ("{ a } } mutation { b }", "mutation"),
        ("query Q($l: [In] = [{a: 1}]) { x } mutation { y }", "mutation"),
        ("query mutation { a }", None),
        ("query { mutation }", None),
        ("# mutation\n{ viewer { login } }", None),
        ('query { a(x: "} mutation {") }', None),
        ("{ a } fragment F on Repository { b }", None),
        ("query Q($f: In = {a: 1}) { x }", None),
    ],
)
def test_write_operation_reads_graphql_tokens(
    document: str, expected: str | None
) -> None:
    from github_statistics.client.policy import write_operation

    assert write_operation(document) == expected


async def test_batched_stops_once_timeouts_take_too_long() -> None:
    from github_statistics.client import GraphQLError, batched

    now = [0.0]
    calls: list[list[int]] = []

    async def fetch(chunk: list[int]) -> list[int]:
        calls.append(chunk)
        now[0] += 200.0
        if chunk[0] > 2:
            raise GraphQLError("timed out", status=504)
        return chunk

    out = await batched(list(range(1, 7)), 2, fetch, str, clock=lambda: now[0])
    assert calls == [[1, 2], [3, 4], [3]]
    assert out.results == [[1, 2]]
    assert not out.failed
    assert out.remaining == [3, 4, 5, 6]


async def test_rate_limit_backoff_holds_queued_requests() -> None:
    import asyncio

    events: list[str] = []

    class Yielding(Sleeper):
        """A sleeper that lets other tasks run while it sleeps."""

        async def __call__(self, delay: float) -> None:
            """Record ``delay`` and yield to the event loop for a while."""
            await super().__call__(delay)
            events.append("sleep")
            for _ in range(20):
                await asyncio.sleep(0)
            events.append("wake")

    answers = iter(
        [httpx.Response(429, headers={"Retry-After": "5"}), httpx.Response(200)]
    )

    def respond(request: httpx.Request) -> httpx.Response:
        events.append(f"send {request.url.path}")
        return next(answers, httpx.Response(200))

    sleep = Yielding()
    with respx.mock:
        respx.get(path__regex=r"^/[ab]$").mock(side_effect=respond)
        async with _transport(sleep, concurrency=1) as transport:
            first, second = await asyncio.gather(
                transport.request("GET", "/a"), transport.request("GET", "/b")
            )
    assert first.status_code == second.status_code == 200
    assert sleep.delays == [5.0]
    assert events[:3] == ["send /a", "sleep", "wake"]
    assert sorted(events[3:]) == ["send /a", "send /b"]


@pytest.mark.parametrize(
    ("path", "graphql", "expected"),
    [
        ("/api/v3/repos/o/r", False, "core"),
        ("/api/v3/search/issues", False, "search"),
        ("/api/v3/repos/search/issues", False, "core"),
        ("/api/graphql", True, "graphql"),
    ],
)
def test_cooldown_resource(path: str, graphql: bool, expected: str) -> None:
    from github_statistics.client.cooldown import Cooldown

    target = httpx.URL(f"https://ghe.example{path}")
    assert Cooldown("/api/v3").resource(target, graphql) == expected


async def test_cooldown_wait_rechecks_the_hold_after_waking() -> None:
    import asyncio

    from github_statistics.client.cooldown import Cooldown

    loop = asyncio.get_running_loop()
    cooldown = Cooldown("")
    first: asyncio.Future[None] = loop.create_future()
    cooldown.pause("core", first)
    waiter = asyncio.create_task(cooldown.wait("core"))
    await asyncio.sleep(0)
    first.set_result(None)
    second: asyncio.Future[None] = loop.create_future()
    cooldown.pause("core", second)
    for _ in range(5):
        await asyncio.sleep(0)
    assert not waiter.done()
    second.set_result(None)
    await asyncio.wait_for(waiter, 1)


TIMEOUT_MESSAGE = (
    "Something went wrong while executing your query. This may be the "
    "result of a timeout, or it could be a GitHub bug."
)


async def test_batched_counts_timeout_diagnostics_in_error_payloads() -> None:
    from github_statistics.client import GraphQLError, batched, graphql

    calls: list[list[int]] = []

    async def fetch(chunk: list[int]) -> list[int]:
        calls.append(chunk)
        raise GraphQLError("failed", errors=[{"message": TIMEOUT_MESSAGE}])

    out = await batched(list(range(1, 5)), 2, fetch, str, max_timeouts=2)
    assert calls == [[1, 2], [1]]
    assert not out.results and not out.failed
    assert out.remaining == [1, 2, 3, 4]
    with respx.mock:
        respx.post(f"{API}/graphql").respond(
            200, json={"errors": [{"message": TIMEOUT_MESSAGE}]}
        )
        async with _transport(Sleeper()) as transport:
            with pytest.raises(GraphQLError) as failure:
                await graphql(transport, "query { x }")
    assert not failure.value.request_wide


async def test_terminal_rate_limit_stops_its_resource() -> None:
    from github_statistics.client import BudgetExhaustedError

    primary = {"Retry-After": "600", "x-ratelimit-remaining": "0"}
    with respx.mock:
        core = respx.get(f"{API}/repos/acme/a").respond(429, headers=primary)
        search = respx.get(f"{API}/search/issues").respond(200, json={})
        async with _transport(Sleeper()) as transport:
            first = await transport.request("GET", "/repos/acme/a")
            with pytest.raises(BudgetExhaustedError):
                await transport.request("GET", "/repos/acme/a")
            other = await transport.request("GET", "/search/issues")
    assert first.status_code == 429
    assert core.call_count == 1
    assert other.status_code == 200 and search.call_count == 1


async def test_queued_request_releases_its_slot_during_a_hold() -> None:
    import asyncio

    events: list[str] = []

    class Yielding(Sleeper):
        """A sleeper that lets other tasks run while it sleeps."""

        async def __call__(self, delay: float) -> None:
            """Record ``delay`` and yield to the event loop for a while."""
            await super().__call__(delay)
            events.append("sleep")
            for _ in range(50):
                await asyncio.sleep(0)
            events.append("wake")

    primary = {"Retry-After": "5", "x-ratelimit-remaining": "0"}
    answers = iter([httpx.Response(429, headers=primary)])

    async def respond(request: httpx.Request) -> httpx.Response:
        events.append(f"send {request.url.path}")
        for _ in range(5):
            await asyncio.sleep(0)
        return next(answers, httpx.Response(200))

    sleep = Yielding()
    with respx.mock:
        respx.get(path__regex=r"^/(a|b|search/issues)$").mock(side_effect=respond)
        async with _transport(sleep, concurrency=1) as transport:
            await asyncio.gather(
                transport.request("GET", "/a"),
                transport.request("GET", "/b"),
                transport.request("GET", "/search/issues"),
            )
    assert events.index("send /search/issues") < events.index("wake")
    assert events.index("wake") < events.index("send /b")


async def test_secondary_rate_limit_pauses_and_stops_every_resource() -> None:
    import asyncio

    from github_statistics.client import BudgetExhaustedError

    events: list[str] = []

    class Yielding(Sleeper):
        """A sleeper that lets other tasks run while it sleeps."""

        async def __call__(self, delay: float) -> None:
            """Record ``delay`` and yield to the event loop for a while."""
            await super().__call__(delay)
            events.append("sleep")
            for _ in range(50):
                await asyncio.sleep(0)
            events.append("wake")

    answers = iter([httpx.Response(429, headers={"Retry-After": "5"})])

    async def respond(request: httpx.Request) -> httpx.Response:
        events.append(f"send {request.url.path}")
        for _ in range(5):
            await asyncio.sleep(0)
        return next(answers, httpx.Response(200))

    with respx.mock:
        respx.get(path__regex=r"^/(a|search/issues)$").mock(side_effect=respond)
        query = respx.post(f"{API}/graphql").respond(200, json={"data": {}})
        async with _transport(Yielding(), concurrency=1) as transport:
            await asyncio.gather(
                transport.request("GET", "/a"),
                transport.request("GET", "/search/issues"),
            )
        assert events.index("wake") < events.index("send /search/issues")
        respx.get(f"{API}/b").respond(429, headers={"Retry-After": "600"})
        async with _transport(Sleeper()) as transport:
            first = await transport.request("GET", "/b")
            with pytest.raises(BudgetExhaustedError):
                await transport.request("GET", "/search/issues")
            with pytest.raises(BudgetExhaustedError):
                await transport._post_graphql({"query": "{ x }"}, 0)
    assert first.status_code == 429
    assert not query.called


async def test_rate_limit_pause_outlives_a_cancelled_request() -> None:
    import asyncio

    gate = asyncio.Event()

    class Gated(Sleeper):
        """A sleeper that returns once the test opens the gate."""

        async def __call__(self, delay: float) -> None:
            """Record ``delay`` and wait for the gate."""
            await super().__call__(delay)
            await gate.wait()

    sleep = Gated()
    with respx.mock:
        respx.get(f"{API}/a").respond(429, headers={"Retry-After": "5"})
        other = respx.get(f"{API}/b").respond(200)
        async with _transport(sleep) as transport:
            first = asyncio.create_task(transport.request("GET", "/a"))
            while not sleep.delays:
                await asyncio.sleep(0)
            second = asyncio.create_task(transport.request("GET", "/b"))
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            for _ in range(20):
                await asyncio.sleep(0)
            assert not other.called
            gate.set()
            response = await asyncio.wait_for(second, 1)
    assert response.status_code == 200
    assert sleep.delays == [5.0]


async def test_secondary_rate_limit_403_body_waits_a_minute() -> None:
    sleep = Sleeper()
    secondary = {"message": "You have exceeded a secondary rate limit."}
    with respx.mock:
        route = respx.get(f"{API}/x").mock(
            side_effect=[
                httpx.Response(403, json=secondary),
                httpx.Response(200, json=[]),
            ]
        )
        async with _transport(sleep) as transport:
            response = await transport.request("GET", "/x")
    assert response.status_code == 200
    assert route.call_count == 2
    assert sleep.delays == [60.0]
    assert transport.stats.retries["rate_limit"] == 1


async def test_secondary_rate_limit_backoff_doubles_from_a_minute() -> None:
    sleep = Sleeper()
    secondary = {"message": "You have exceeded a secondary rate limit."}
    with respx.mock:
        route = respx.get(f"{API}/x").mock(
            side_effect=[
                httpx.Response(403, json=secondary),
                httpx.Response(403, json=secondary),
                httpx.Response(403, json=secondary),
                httpx.Response(200, json=[]),
            ]
        )
        async with _transport(sleep, max_wait=500.0) as transport:
            response = await transport.request("GET", "/x")
    assert response.status_code == 200
    assert route.call_count == 4
    assert sleep.delays == [60.0, 120.0, 240.0]


@pytest.mark.parametrize("status", [403, 429])
async def test_secondary_body_outranks_remaining_zero(status: int) -> None:
    from github_statistics.client import BudgetExhaustedError

    secondary = {"message": "You have exceeded a secondary rate limit."}
    headers = {"Retry-After": "600", "x-ratelimit-remaining": "0"}
    with respx.mock:
        respx.get(f"{API}/a").respond(status, headers=headers, json=secondary)
        search = respx.get(f"{API}/search/issues").respond(200, json={})
        async with _transport(Sleeper()) as transport:
            first = await transport.request("GET", "/a")
            with pytest.raises(BudgetExhaustedError):
                await transport.request("GET", "/search/issues")
    assert first.status_code == status
    assert not search.called


async def test_unbudgeted_graphql_payload_rate_limit_stops_graphql() -> None:
    from github_statistics.client import BudgetExhaustedError, GraphQLError, graphql

    limited = {"errors": [{"type": "RATE_LIMITED", "message": "slow down"}]}
    with respx.mock:
        route = respx.post(f"{API}/graphql").respond(200, json=limited)
        rest = respx.get(f"{API}/x").respond(200)
        async with _transport(Sleeper()) as transport:
            with pytest.raises(GraphQLError) as caught:
                await graphql(transport, "query { x }")
            with pytest.raises(BudgetExhaustedError):
                await graphql(transport, "query { x }")
            await transport.request("GET", "/x")
    assert caught.value.request_wide
    assert route.call_count == 1
    assert rest.call_count == 1


async def test_budgeted_terminal_rate_limit_raises_budget_exhausted() -> None:
    from github_statistics.client import Budget, BudgetExhaustedError, get_paged

    budget = Budget()
    with respx.mock:
        route = respx.get(f"{API}/repos/o/r/issues").respond(
            429, headers={"Retry-After": "600"}
        )
        async with _transport(Sleeper(), budget=budget) as transport:
            with pytest.raises(BudgetExhaustedError):
                async for _ in get_paged(transport, "/repos/o/r/issues", {}):
                    pass
    assert route.call_count == 1
    assert budget.rest_left > 0


async def test_graphql_rate_limit_code_stops_the_batch() -> None:
    from github_statistics.client import Budget, batched, graphql

    limited = {
        "data": None,
        "errors": [
            {
                "type": "RATE_LIMIT",
                "extensions": {"code": "graphql_rate_limit"},
                "message": "API rate limit already exceeded for user ID 1.",
            }
        ],
    }
    with respx.mock:
        route = respx.post(f"{API}/graphql").respond(200, json=limited)
        async with _transport(Sleeper(), budget=Budget()) as transport:

            async def fetch(chunk: list[int]) -> list[int]:
                await graphql(transport, "query { x }", cost_estimate=1)
                return chunk

            out = await batched([1, 2, 3], 3, fetch, str)
    assert route.call_count == 1
    assert not out.results and not out.failed
    assert out.remaining == [1, 2, 3]


async def test_primary_graphql_error_in_a_403_stops_only_its_resource() -> None:
    primary = {"errors": [{"type": "RATE_LIMITED", "message": "slow down"}]}
    headers = {"Retry-After": "600", "x-ratelimit-remaining": "0"}
    with respx.mock:
        respx.get(f"{API}/a").respond(403, headers=headers, json=primary)
        search = respx.get(f"{API}/search/issues").respond(200, json={})
        async with _transport(Sleeper()) as transport:
            first = await transport.request("GET", "/a")
            other = await transport.request("GET", "/search/issues")
    assert first.status_code == 403
    assert other.status_code == 200 and search.call_count == 1


async def test_spent_quota_waits_for_its_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from github_statistics.client import Budget, BudgetExhaustedError, policy

    clock = SimpleNamespace(time=lambda: 1000.0)
    monkeypatch.setattr(policy, "time", clock, raising=False)
    sleep = Sleeper()
    with respx.mock:
        respx.get(f"{API}/x").mock(
            side_effect=[
                httpx.Response(
                    403,
                    headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"},
                ),
                httpx.Response(200, json={}),
            ]
        )
        far = respx.get(f"{API}/y").respond(
            403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1600"}
        )
        async with _transport(sleep) as transport:
            response = await transport.request("GET", "/x")
        async with _transport(sleep, budget=Budget()) as transport:
            with pytest.raises(BudgetExhaustedError):
                await transport.request("GET", "/y")
    assert response.status_code == 200
    assert sleep.delays == [30.0]
    assert far.call_count == 1
