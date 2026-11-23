# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for review, merger and closer enrichment (design §6.3)."""

from __future__ import annotations

import dataclasses
import importlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

GRAPHQL = "https://api.github.com/graphql"
REPO = "acme/a"
ALICE = {"login": "alice", "__typename": "User"}
BOT = {"login": "lf-releng-code-monkey-bot", "__typename": "Bot"}

pytestmark = pytest.mark.xfail(strict=True, reason="enrichment not implemented")


def _api() -> Any:
    """The collector, loaded when a test runs so a missing one fails that test."""
    return importlib.import_module("github_statistics.collect")


def _at(day: int, hour: int = 12) -> datetime:
    """A UTC timestamp on ``day`` March 2026."""
    return datetime(2026, 3, day, hour, tzinfo=UTC)


def _item(
    number: int,
    *,
    pr: bool = False,
    updated: datetime | None = None,
    closed: datetime | None = None,
    merged: datetime | None = None,
) -> Any:
    """A stored issue, or a pull request with ``pr``, never enriched."""
    from github_statistics.store import Item

    return Item(
        repo=REPO,
        number=number,
        node_id=f"{'PR' if pr else 'I'}_{number}",
        kind="pr" if pr else "issue",
        created_at=_at(1),
        closed_at=closed,
        merged_at=merged,
        author="alice",
        author_type="User",
        updated_at=updated or _at(2),
    )


def _store(*items: Any) -> Any:
    """An in-memory store holding ``items``."""
    from github_statistics.store import Store

    store = Store(Path(":memory:"))
    store.upsert_items(items)
    return store


def _review(review_id: int, state: str = "COMMENTED", author: Any = BOT) -> Any:
    """One review node as GraphQL returns it; a ``BigInt`` is a string."""
    return {
        "id": f"PRR_{review_id}",
        "fullDatabaseId": str(review_id),
        "author": author,
        "state": state,
        "submittedAt": None if state == "PENDING" else "2026-03-03T10:00:00Z",
    }


def _closed(actor: Any) -> dict[str, Any]:
    """A ``timelineItems`` with one ``CLOSED_EVENT`` by ``actor``."""
    return {"nodes": [{"actor": actor}]}


def _pr(
    number: int,
    reviews: list[Any] | None = None,
    *,
    merged_by: Any = None,
    more: str | None = None,
) -> dict[str, Any]:
    """A pull request node; ``more`` is the cursor of a further review page."""
    nodes = reviews or []
    return {
        "__typename": "PullRequest",
        "id": f"PR_{number}",
        "number": number,
        "repository": {"nameWithOwner": REPO},
        "mergedBy": merged_by,
        "reviews": {
            "totalCount": len(nodes) + (1 if more else 0),
            "pageInfo": {"hasNextPage": more is not None, "endCursor": more},
            "nodes": nodes,
        },
        "timelineItems": _closed(merged_by) if merged_by else {"nodes": []},
    }


def _issue(number: int, closer: Any = ALICE, closed: bool = True) -> dict[str, Any]:
    """An issue node, closed by ``closer`` when ``closed``."""
    return {
        "__typename": "Issue",
        "id": f"I_{number}",
        "number": number,
        "repository": {"nameWithOwner": REPO},
        "timelineItems": _closed(closer) if closed else {"nodes": []},
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


async def _instant(_: float) -> None:
    return None


async def _enrich(store: Any, budget: Any = None) -> Any:
    """Run ``enrich_items`` over ``acme/a``."""
    from github_statistics.client import Transport

    async with Transport(
        None, sleep=_instant, max_retries=0, budget=budget
    ) as transport:
        return await _api().enrich_items(transport, store, [REPO])


def _stored(store: Any, number: int) -> Any:
    """The stored item ``number`` of ``acme/a``."""
    rows = store.connection.execute(
        "SELECT merged_by, merged_by_type, closed_by, closed_by_type,"
        " reviews_read_at FROM items WHERE repo = ? AND number = ?",
        (REPO, number),
    )
    return tuple(rows.fetchone())


def _reviews(store: Any, number: int) -> list[tuple[Any, ...]]:
    return [
        (r.id, r.node_id, r.reviewer, r.reviewer_type, r.state)
        for r in store.reviews_for(REPO, number)
    ]


@respx.mock
async def test_mixed_batch_stores_reviews_mergers_and_closers() -> None:
    merged = _item(1, pr=True, updated=_at(5), closed=_at(4), merged=_at(4))
    closed = _item(2, updated=_at(3), closed=_at(3))
    open_issue = _item(3, updated=_at(4))
    nodes = {
        "PR_1": _pr(1, [_review(10)], merged_by=ALICE),
        "I_2": _issue(2, closer=BOT),
        "I_3": _issue(3, closed=False),
    }
    route = _serve(_data(nodes))
    with _store(merged, closed, open_issue) as store:
        result = await _enrich(store)

        assert route.call_count == 1
        ids = json.loads(route.calls[0].request.content)["variables"]["ids"]
        assert ids == ["PR_1", "I_3", "I_2"]
        assert (result.items, result.reviews) == (3, 1)
        assert result.failed == () and result.remaining == ()
        assert _stored(store, 1) == (
            "alice",
            "User",
            "alice",
            "User",
            "2026-03-05T12:00:00Z",
        )
        assert _stored(store, 2) == (
            None,
            None,
            "lf-releng-code-monkey-bot",
            "Bot",
            "2026-03-03T12:00:00Z",
        )
        assert _stored(store, 3)[:4] == (None, None, None, None)
        assert _reviews(store, 1) == [
            (10, "PRR_10", "lf-releng-code-monkey-bot", "Bot", "COMMENTED")
        ]
        assert store.items_needing_enrichment(REPO, 10) == []


@respx.mock
async def test_deleted_accounts_read_as_ghost_and_missing_closer_stays_null() -> None:
    merged = _item(1, pr=True, closed=_at(2), merged=_at(2))
    closed = _item(2, closed=_at(2))
    unknown = _item(3, closed=_at(2))
    pr = _pr(1, [_review(10, author=None)])
    pr["timelineItems"] = _closed(None)
    nodes = {"PR_1": pr, "I_2": _issue(2, closer=None), "I_3": _issue(3, closed=False)}
    _serve(_data(nodes))
    with _store(merged, closed, unknown) as store:
        await _enrich(store)

        assert _stored(store, 1)[:4] == ("ghost", "User", "ghost", "User")
        assert _stored(store, 2)[:4] == (None, None, "ghost", "User")
        assert _stored(store, 3)[:4] == (None, None, None, None)
        assert _reviews(store, 1) == [(10, "PRR_10", "ghost", "User", "COMMENTED")]


@respx.mock
async def test_missing_nodes_are_marked_read_and_keep_stored_fields() -> None:
    known = dataclasses.replace(
        _item(1, closed=_at(2)), closed_by="alice", closed_by_type="User"
    )
    gone = _item(2, updated=_at(3))

    def answer(variables: dict[str, Any]) -> httpx.Response:
        ids = variables["ids"]
        if "I_2" in ids:
            found = [None if i == "I_2" else _issue(1, closer=None) for i in ids]
            error = {"type": "NOT_FOUND", "path": ["nodes", ids.index("I_2")]}
            return httpx.Response(
                200, json={"data": {"nodes": found}, "errors": [error]}
            )
        return httpx.Response(200, json={"data": {"nodes": [None] * len(ids)}})

    route = _serve(answer)
    with _store(known, gone) as store:
        result = await _enrich(store)

        assert route.call_count == 3
        assert result.items == 2 and result.failed == ()
        assert _stored(store, 1) == (
            None,
            None,
            "alice",
            "User",
            "2026-03-02T12:00:00Z",
        )
        assert _stored(store, 2)[4] == "2026-03-03T12:00:00Z"


@respx.mock
async def test_more_than_fifty_reviews_page_for_that_pull_request() -> None:
    first = [_review(i) for i in range(1, 51)]

    def answer(variables: dict[str, Any]) -> httpx.Response:
        if "ids" in variables:
            return _data({"PR_1": _pr(1, first, more="c1")})(variables)
        assert variables == {"id": "PR_1", "cursor": "c1"}
        page = {
            "pageInfo": {"hasNextPage": False, "endCursor": "c2"},
            "nodes": [_review(51, "APPROVED")],
        }
        return httpx.Response(200, json={"data": {"node": {"reviews": page}}})

    route = _serve(answer)
    with _store(_item(1, pr=True)) as store:
        result = await _enrich(store)

        assert route.call_count == 2
        assert result.reviews == 51
        assert len(store.reviews_for(REPO, 1)) == 51
        assert _reviews(store, 1)[-1][4] == "APPROVED"


@respx.mock
async def test_pending_reviews_are_skipped() -> None:
    reviews = [_review(1, "PENDING"), _review(2, "APPROVED")]
    _serve(_data({"PR_1": _pr(1, reviews)}))
    with _store(_item(1, pr=True)) as store:
        await _enrich(store)

        assert [r[0] for r in _reviews(store, 1)] == [2]


@respx.mock
async def test_review_id_beyond_32_bits_is_stored() -> None:
    _serve(_data({"PR_1": _pr(1, [_review(6_015_267_803)])}))
    with _store(_item(1, pr=True)) as store:
        result = await _enrich(store)

        assert result.reviews == 1
        assert [r[0] for r in _reviews(store, 1)] == [6_015_267_803]


@respx.mock
async def test_updated_item_is_read_again_and_its_reviews_replaced() -> None:
    nodes = {"PR_1": _pr(1, [_review(1, "APPROVED"), _review(2)])}
    route = _serve(_data(nodes))
    with _store(_item(1, pr=True, updated=_at(2))) as store:
        await _enrich(store)
        assert await _enrich(store) == _api().EnrichResult(0, 0)
        assert route.call_count == 1

        store.upsert_items([_item(1, pr=True, updated=_at(6))])
        nodes["PR_1"] = _pr(1, [_review(1, "DISMISSED")])
        await _enrich(store)

        assert route.call_count == 2
        assert [(r[0], r[4]) for r in _reviews(store, 1)] == [(1, "DISMISSED")]
        assert _stored(store, 1)[4] == "2026-03-06T12:00:00Z"


@respx.mock
async def test_spent_budget_stops_cleanly_newest_first() -> None:
    from github_statistics.client import Budget

    items = [
        _item(n, updated=datetime(2026, 3, 1, tzinfo=UTC).replace(minute=n))
        for n in range(51)
    ]
    nodes = {f"I_{n}": _issue(n, closed=False) for n in range(51)}
    budget = Budget(graphql_points=2)
    route = _serve(_data(nodes, cost=2))
    with _store(*items) as store:
        result = await _enrich(store, budget)

        assert route.call_count == 1
        assert result.items == 50
        assert result.remaining == ("I_0",)
        assert budget.graphql_spent == 2
        assert [i.number for i in store.items_needing_enrichment(REPO, 10)] == [0]


@respx.mock
async def test_last_point_reads_one_item_so_runs_progress() -> None:
    from github_statistics.client import Budget

    items = [_item(n, updated=_at(10 - n)) for n in range(1, 4)]
    nodes = {f"I_{n}": _issue(n, closed=False) for n in range(1, 4)}
    route = _serve(_data(nodes))
    with _store(*items) as store:
        for run, number in enumerate((1, 2, 3), start=1):
            budget = Budget(graphql_points=1)
            result = await _enrich(store, budget)

            ids = json.loads(route.calls[-1].request.content)["variables"]["ids"]
            assert ids == [f"I_{number}"]
            assert route.call_count == run
            assert result.items == 1 and budget.graphql_spent == 1
            assert result.remaining == tuple(f"I_{n}" for n in range(number + 1, 4))
        assert store.items_needing_enrichment(REPO, 10) == []


@respx.mock
async def test_batch_is_halved_after_a_502() -> None:
    nodes = {"I_1": _issue(1), "I_2": _issue(2)}
    answer = _data(nodes)

    def flaky(variables: dict[str, Any]) -> httpx.Response:
        if len(variables["ids"]) > 1:
            return httpx.Response(502)
        return answer(variables)

    route = _serve(flaky)
    with _store(_item(1, closed=_at(2)), _item(2, closed=_at(2))) as store:
        result = await _enrich(store)

        assert route.call_count == 3
        assert result.items == 2
        assert store.items_needing_enrichment(REPO, 10) == []


@respx.mock
async def test_id_failing_alone_is_left_for_the_next_run() -> None:
    def answer(variables: dict[str, Any]) -> httpx.Response:
        error = {"type": "FORBIDDEN", "message": "no"}
        return httpx.Response(200, json={"data": {"nodes": [None]}, "errors": [error]})

    _serve(answer)
    with _store(_item(1)) as store:
        result = await _enrich(store)

        assert result.items == 0
        assert result.failed == ("I_1",)
        assert [i.number for i in store.items_needing_enrichment(REPO, 10)] == [1]


@respx.mock
async def test_review_page_without_node_keeps_stored_reviews() -> None:
    from github_statistics.store import Review

    def answer(variables: dict[str, Any]) -> httpx.Response:
        if "ids" in variables:
            return _data({"PR_1": _pr(1, [_review(2)], more="c1")})(variables)
        return httpx.Response(200, json={"data": {"node": None}})

    _serve(answer)
    with _store(_item(1, pr=True)) as store:
        kept = Review(1, "PRR_1", REPO, 1, "alice", "User", "APPROVED", _at(2))
        store.replace_reviews(REPO, 1, [kept])
        result = await _enrich(store)

        assert result.items == 0 and result.failed == ("PR_1",)
        assert [r[0] for r in _reviews(store, 1)] == [1]
        assert [i.number for i in store.items_needing_enrichment(REPO, 10)] == [1]


@respx.mock
async def test_nested_not_found_fails_the_item_instead_of_marking_it() -> None:
    def answer(variables: dict[str, Any]) -> httpx.Response:
        error = {"type": "NOT_FOUND", "path": ["nodes", 0, "mergedBy"]}
        found = [_pr(1)] * len(variables["ids"])
        return httpx.Response(200, json={"data": {"nodes": found}, "errors": [error]})

    _serve(answer)
    with _store(_item(1, pr=True)) as store:
        result = await _enrich(store)

        assert result.items == 0 and result.failed == ("PR_1",)
        assert [i.number for i in store.items_needing_enrichment(REPO, 10)] == [1]


@respx.mock
async def test_budget_spent_while_paging_keeps_finished_items() -> None:
    from github_statistics.client import Budget

    items = [_item(n, pr=True, updated=_at(10 - n)) for n in range(1, 5)]
    nodes = {f"PR_{n}": _pr(n, [_review(n)], more=f"c{n}") for n in range(1, 5)}

    def answer(variables: dict[str, Any]) -> httpx.Response:
        if "ids" in variables:
            return _data(nodes)(variables)
        n = int(variables["id"].removeprefix("PR_"))
        page = {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [_review(10 + n)],
        }
        rate = {"cost": 1, "remaining": 4999}
        return httpx.Response(
            200, json={"data": {"rateLimit": rate, "node": {"reviews": page}}}
        )

    _serve(answer)
    with _store(*items) as store:
        first = await _enrich(store, Budget(graphql_points=3))
        assert (first.items, first.reviews) == (2, 4)
        assert first.remaining == ("PR_3", "PR_4")
        assert [r[0] for r in _reviews(store, 1)] == [1, 11]

        second = await _enrich(store, Budget(graphql_points=3))
        assert second.items == 2 and second.remaining == ()
        assert store.items_needing_enrichment(REPO, 10) == []


@respx.mock
async def test_long_review_history_never_holds_back_shorter_items(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from github_statistics.client import Budget
    from github_statistics.store import Review

    long = _pr(1, [_review(1)], more="c")
    long["reviews"]["totalCount"] = 2_000
    nodes = {"PR_1": long, "PR_2": _pr(2, [_review(20)], more="c")}
    nodes |= {f"I_{n}": _issue(n) for n in (3, 4)}

    def answer(variables: dict[str, Any]) -> httpx.Response:
        if "ids" in variables:
            return _data(nodes)(variables)
        n = int(variables["id"].removeprefix("PR_"))
        page = {
            "pageInfo": {"hasNextPage": n == 1, "endCursor": "c"},
            "nodes": [_review(10 * n + 1)],
        }
        rate = {"cost": 1, "remaining": 4999}
        return httpx.Response(
            200, json={"data": {"rateLimit": rate, "node": {"reviews": page}}}
        )

    _serve(answer)
    with _store(_item(1, pr=True, updated=_at(9))) as store:
        kept = Review(5, "PRR_5", REPO, 1, "alice", "User", "APPROVED", _at(2))
        store.replace_reviews(REPO, 1, [kept])
        for day in (5, 6):
            others = [_item(2, pr=True, updated=_at(day))]
            others += [_item(n, closed=_at(2), updated=_at(day - 1)) for n in (3, 4)]
            store.upsert_items(others)
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                result = await _enrich(store, Budget(graphql_points=10))

            assert (result.items, result.remaining) == (3, ("PR_1",))
            assert result.failed == ()
            assert [r[0] for r in _reviews(store, 2)] == [20, 21]
            assert [r[0] for r in _reviews(store, 1)] == [5]
            assert "acme/a#1" in caplog.text
            assert "budget.graphql_points" not in caplog.text
            assert [i.number for i in store.items_needing_enrichment(REPO, 10)] == [1]


@respx.mock
async def test_budget_completing_no_item_names_the_setting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from github_statistics.client import Budget

    long = _pr(1, [_review(1)], more="c")
    route = _serve(_data({"PR_1": long}, cost=10))
    with _store(_item(1, pr=True)) as store:
        with caplog.at_level(logging.WARNING):
            result = await _enrich(store, Budget(graphql_points=10))

        assert route.call_count == 1
        assert result.items == 0 and result.remaining == ("PR_1",)
        assert caplog.text.count("budget.graphql_points") == 1


@pytest.mark.parametrize(
    "outage",
    [
        httpx.Response(502),
        httpx.Response(
            200,
            json={"errors": [{"message": "This may be the result of a timeout"}]},
        ),
    ],
    ids=["502", "timeout"],
)
@respx.mock
async def test_failing_review_page_ends_the_paging(
    outage: httpx.Response, caplog: pytest.LogCaptureFixture
) -> None:
    nodes = {f"PR_{n}": _pr(n, [_review(n)], more="c") for n in (1, 2, 3)}
    nodes["I_4"] = _issue(4)
    for n in (1, 2, 3):
        nodes[f"PR_{n}"]["reviews"]["totalCount"] = 50 + n

    def answer(variables: dict[str, Any]) -> httpx.Response:
        if "ids" in variables:
            return _data(nodes)(variables)
        return outage

    route = _serve(answer)
    items = [_item(n, pr=True, updated=_at(10 - n)) for n in (1, 2, 3)]
    with _store(*items, _item(4, closed=_at(2))) as store:
        with caplog.at_level(logging.WARNING):
            result = await _enrich(store)

        assert route.call_count == 2
        assert (result.items, result.failed) == (1, ())
        assert result.remaining == ("PR_1", "PR_2", "PR_3")
        assert caplog.text.count("stopped paging reviews at acme/a#1") == 1
        assert [i.number for i in store.items_needing_enrichment(REPO, 10)] == [
            1,
            2,
            3,
        ]


@respx.mock
async def test_rows_sharing_a_node_id_are_read_once_and_all_marked() -> None:
    stale = dataclasses.replace(_item(1, pr=True, updated=_at(2)), node_id="PR_7")
    owner = _item(7, pr=True, updated=_at(5))
    route = _serve(_data({"PR_7": _pr(7, [_review(70)])}))
    with _store(stale, owner) as store:
        result = await _enrich(store)

        assert route.call_count == 1
        assert json.loads(route.calls[0].request.content)["variables"]["ids"] == [
            "PR_7"
        ]
        assert (result.items, result.reviews) == (2, 1)
        assert result.failed == () and result.remaining == ()
        assert [r[0] for r in _reviews(store, 7)] == [70]
        assert _reviews(store, 1) == []
        assert store.items_needing_enrichment(REPO, 10) == []


@respx.mock
async def test_stale_row_never_takes_an_already_read_owners_reviews() -> None:
    from github_statistics.store import Enrichment, Review

    owner = _item(7, pr=True, updated=_at(5))
    stale = dataclasses.replace(_item(1, pr=True, updated=_at(2)), node_id="PR_7")
    _serve(_data({"PR_7": _pr(7, [_review(70, "DISMISSED")])}))
    with _store(owner, stale) as store:
        kept = Review(70, "PRR_70", REPO, 7, "alice", "User", "APPROVED", _at(4))
        mark = Enrichment(REPO, 7, None, None, None, None, owner.updated_at)
        store.store_enrichments([mark], {(REPO, 7): [kept]})
        result = await _enrich(store)

        assert (result.items, result.reviews) == (1, 0)
        assert [(r[0], r[4]) for r in _reviews(store, 7)] == [(70, "APPROVED")]
        assert _reviews(store, 1) == []
        assert _stored(store, 1)[4] == "2026-03-02T12:00:00Z"
        assert store.items_needing_enrichment(REPO, 10) == []
