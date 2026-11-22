# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the issue and pull request collector (design §6.2)."""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

pytestmark = pytest.mark.xfail(strict=True, reason="item collector not built yet")

API = "https://api.github.com"
LISTING = f"{API}/repos/acme/a/issues"
WINDOW = date(2026, 3, 1)
NOW = datetime(2026, 3, 10, 6, tzinfo=UTC)
FIRST_SINCE = "2026-02-28T23:59:59Z"
ALICE = {"login": "alice", "type": "User"}


def _api() -> Any:
    """The collector, loaded when a test runs so a missing one fails that test."""
    return importlib.import_module("github_statistics.collect")


def _at(day: int, hour: int = 12, second: int = 0) -> datetime:
    """A UTC timestamp on ``day`` March 2026."""
    return datetime(2026, 3, day, hour, 0, second, tzinfo=UTC)


def _text(value: datetime) -> str:
    """``value`` as GitHub writes a timestamp."""
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _listed(
    number: int,
    updated: datetime,
    *,
    merged: datetime | None = None,
    pr: bool = False,
    closed: datetime | None = None,
    user: dict[str, str] | None = ALICE,
) -> dict[str, Any]:
    """An issue, or a pull request with ``pr``, as the listing returns it."""
    data: dict[str, Any] = {
        "number": number,
        "node_id": f"{'PR' if pr else 'I'}_{number}",
        "created_at": _text(_at(1)),
        "updated_at": _text(updated),
        "closed_at": None if closed is None else _text(closed),
        "user": user,
    }
    if pr:
        data["pull_request"] = {"merged_at": None if merged is None else _text(merged)}
    return data


def _serve(pages: dict[str, list[list[dict[str, Any]]]]) -> respx.Route:
    """Mock the listing: ``pages[since]`` in order, each linked to the next."""

    def respond(request: httpx.Request) -> httpx.Response:
        chunks = pages[request.url.params["since"]]
        page = int(request.url.params.get("page", "1"))
        headers = {}
        if page < len(chunks):
            following = request.url.copy_merge_params({"page": page + 1})
            headers["link"] = f'<{following}>; rel="next"'
        return httpx.Response(200, json=chunks[page - 1], headers=headers)

    return respx.get(LISTING).mock(side_effect=respond)


def _store() -> Any:
    """An in-memory store."""
    from github_statistics.store import Store

    return Store(Path(":memory:"))


async def _instant(_: float) -> None:
    return None


async def _sync(store: Any, budget: Any = None, repo: str = "a") -> Any:
    """Run ``sync_items`` for ``acme/<repo>`` with ``budget`` on its transport."""
    from github_statistics.client import Transport

    async with Transport(
        None, sleep=_instant, max_retries=0, budget=budget
    ) as transport:
        return await _api().sync_items(
            transport,
            store,
            "acme",
            repo,
            window_start=WINDOW,
            now=NOW,
        )


def _coverage(store: Any, metric: str) -> list[tuple[date, date]]:
    return [(r.from_day, r.to_day) for r in store.coverage("acme/a", metric)]


def _watermark(store: Any, repo: str = "acme/a") -> datetime | None:
    state = store.sync_state(repo, "items")
    return None if state is None else state.watermark


@respx.mock
async def test_first_sync_lists_from_the_window_start_and_covers_it() -> None:
    route = _serve({FIRST_SINCE: [[_listed(1, _at(4)), _listed(2, _at(5))]]})
    with _store() as store:
        result = await _sync(store)

        params = route.calls[0].request.url.params
        assert dict(params) == {
            "state": "all",
            "sort": "updated",
            "direction": "asc",
            "per_page": "100",
            "since": FIRST_SINCE,
        }
        assert (result.repo, result.items, result.pages) == ("acme/a", 2, 1)
        assert result.drained is True and result.unreadable is None
        assert _watermark(store) == _at(5)
        for metric in ("issues", "pull_requests"):
            assert _coverage(store, metric) == [(WINDOW, date(2026, 3, 9))]


@respx.mock
async def test_incremental_sync_starts_one_second_before_the_watermark() -> None:
    from github_statistics.store import SyncState

    route = _serve({"2026-03-05T11:59:59Z": [[]]})
    with _store() as store:
        oldest = datetime(2026, 2, 28, 23, 59, 59, tzinfo=UTC)
        store.set_sync_state(
            SyncState(
                "acme/a", "items", watermark=_at(5), oldest=oldest, last_sync_at=_at(5)
            )
        )
        for metric in ("issues", "pull_requests"):
            store.add_coverage("acme/a", metric, WINDOW, date(2026, 3, 5))

        result = await _sync(store)

        assert route.call_count == 1
        assert result.drained is True and result.items == 0
        assert _watermark(store) == _at(5)
        for metric in ("issues", "pull_requests"):
            assert _coverage(store, metric) == [(WINDOW, date(2026, 3, 9))]


@respx.mock
async def test_interrupted_sync_resumes_without_losing_the_boundary_item() -> None:
    from github_statistics.client import Budget

    boundary = _at(5)
    _serve(
        {
            FIRST_SINCE: [
                [_listed(1, _at(4)), _listed(2, boundary)],
                [_listed(3, boundary)],
            ],
            "2026-03-05T11:59:59Z": [[_listed(2, boundary), _listed(3, boundary)]],
        }
    )
    with _store() as store:
        cut = await _sync(store, budget=Budget(rest=1))

        assert (cut.drained, cut.pages, cut.items) == (False, 1, 2)
        assert _watermark(store) == boundary
        assert store.counts()["items"] == 2
        assert _coverage(store, "issues") == []
        assert _coverage(store, "pull_requests") == []

        resumed = await _sync(store)

        assert resumed.drained is True
        assert store.counts()["items"] == 3
        assert _coverage(store, "issues") == [(WINDOW, date(2026, 3, 9))]


@respx.mock
async def test_interrupted_first_sync_restarts_when_the_window_starts_earlier() -> None:
    from github_statistics.client import Budget, Transport

    earlier = date(2026, 2, 1)
    route = _serve(
        {
            FIRST_SINCE: [[_listed(1, _at(4))], [_listed(2, _at(5))]],
            "2026-01-31T23:59:59Z": [[_listed(3, datetime(2026, 2, 2, tzinfo=UTC))]],
        }
    )
    with _store() as store:
        await _sync(store, budget=Budget(rest=1))
        async with Transport(None, sleep=_instant, max_retries=0) as transport:
            result = await _api().sync_items(
                transport, store, "acme", "a", window_start=earlier, now=NOW
            )

        assert route.calls[-1].request.url.params["since"] == "2026-01-31T23:59:59Z"
        assert result.drained is True
        assert store.counts()["items"] == 2
        assert _coverage(store, "issues") == [(earlier, date(2026, 3, 9))]


@respx.mock
async def test_interrupted_first_sync_restarts_after_a_prune_drops_its_items() -> None:
    from github_statistics.client import Budget, Transport

    window, since = date(2026, 3, 3), "2026-03-02T23:59:59Z"
    closed = _listed(1, _at(3), closed=_at(3))
    route = _serve(
        {
            FIRST_SINCE: [[closed, _listed(2, _at(6))], [_listed(3, _at(7))]],
            since: [[closed, _listed(2, _at(6)), _listed(3, _at(7))]],
        }
    )
    with _store() as store:
        await _sync(store, budget=Budget(rest=1))
        store.prune(date(2026, 3, 5))
        async with Transport(None, sleep=_instant, max_retries=0) as transport:
            result = await _api().sync_items(
                transport, store, "acme", "a", window_start=window, now=NOW
            )

        assert route.calls[-1].request.url.params["since"] == since
        assert result.drained is True
        assert store.counts()["items"] == 3
        assert _coverage(store, "issues") == [(window, date(2026, 3, 9))]


@respx.mock
async def test_interrupted_first_sync_restarts_when_the_window_moves_past_it() -> None:
    from github_statistics.client import Budget, Transport

    window, since = date(2026, 3, 6), "2026-03-05T23:59:59Z"
    route = _serve(
        {
            FIRST_SINCE: [[_listed(1, _at(4))], [_listed(2, _at(5))]],
            since: [[_listed(3, _at(7))]],
        }
    )
    with _store() as store:
        await _sync(store, budget=Budget(rest=1))
        async with Transport(None, sleep=_instant, max_retries=0) as transport:
            result = await _api().sync_items(
                transport, store, "acme", "a", window_start=window, now=NOW
            )

        assert route.calls[-1].request.url.params["since"] == since
        assert result.drained is True and result.items == 1
        assert _coverage(store, "issues") == [(window, date(2026, 3, 9))]


@pytest.mark.parametrize("change", ["wider window", "prune"])
@respx.mock
async def test_finished_sync_restarts_when_the_window_starts_before_its_scan(
    change: str,
) -> None:
    from github_statistics.client import Transport

    window, since = date(2026, 2, 1), "2026-01-31T23:59:59Z"
    if change == "prune":
        window, since = date(2026, 3, 3), "2026-03-02T23:59:59Z"
    route = _serve({FIRST_SINCE: [[]], since: [[]]})
    with _store() as store:
        await _sync(store)
        if change == "prune":
            store.prune(date(2026, 3, 5))
        async with Transport(None, sleep=_instant, max_retries=0) as transport:
            result = await _api().sync_items(
                transport, store, "acme", "a", window_start=window, now=NOW
            )

        assert route.calls[-1].request.url.params["since"] == since
        assert result.drained is True
        for metric in ("issues", "pull_requests"):
            assert _coverage(store, metric) == [(window, date(2026, 3, 9))]


@respx.mock
async def test_finished_sync_checks_its_watermark_and_oldest_not_coverage() -> None:
    from github_statistics.client import Transport

    later, later_since = date(2026, 3, 6), "2026-03-05T23:59:59Z"
    wider, wider_since = date(2026, 3, 3), "2026-03-02T23:59:59Z"
    route = _serve(
        {
            FIRST_SINCE: [[_listed(1, _at(4)), _listed(2, _at(5))]],
            later_since: [[_listed(3, _at(7))]],
            wider_since: [[_listed(1, _at(4)), _listed(2, _at(5)), _listed(3, _at(7))]],
        }
    )
    with _store() as store:
        await _sync(store)
        for window in (later, wider):
            async with Transport(None, sleep=_instant, max_retries=0) as transport:
                result = await _api().sync_items(
                    transport, store, "acme", "a", window_start=window, now=NOW
                )
            assert result.drained is True

        sinces = [call.request.url.params["since"] for call in route.calls]
        assert sinces == [FIRST_SINCE, later_since, wider_since]
        assert store.sync_state("acme/a", "items").oldest == datetime(
            2026, 3, 2, 23, 59, 59, tzinfo=UTC
        )


@pytest.mark.xfail(strict=True, reason="reread_reviews does not exist yet")
@respx.mock
async def test_a_wider_window_rereads_reviews_a_prune_dropped() -> None:
    from github_statistics.client import Transport
    from github_statistics.store import Item, SyncState

    def pr(number: int, created: datetime, kind: str = "pr") -> Item:
        return Item(
            repo="acme/a",
            number=number,
            node_id=f"PR_{number}",
            kind=kind,
            created_at=created,
            closed_at=None,
            merged_at=None,
            author="alice",
            author_type="User",
            updated_at=_at(7),
            reviews_read_at=_at(7),
        )

    route = _serve({"2026-03-02T23:59:59Z": [[]]})
    with _store() as store:
        store.upsert_items([pr(1, _at(1)), pr(2, _at(6)), pr(3, _at(1), "issue")])
        store.set_sync_state(
            SyncState(
                "acme/a", "items", watermark=_at(7), oldest=_at(1), last_sync_at=_at(7)
            )
        )
        store.prune(date(2026, 3, 5))
        async with Transport(None, sleep=_instant, max_retries=0) as transport:
            await _api().sync_items(
                transport, store, "acme", "a", window_start=date(2026, 3, 3), now=NOW
            )

        assert route.call_count == 1
        assert [i.number for i in store.items_needing_enrichment("acme/a", 10)] == [1]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_a_wider_window_stores_its_restart_before_the_first_page() -> None:
    from github_statistics.client import Budget
    from github_statistics.store import Enrichment, Item, SyncState

    read = Item(
        repo="acme/a",
        number=1,
        node_id="PR_1",
        kind="pr",
        created_at=_at(1),
        closed_at=None,
        merged_at=None,
        author="alice",
        author_type="User",
        updated_at=_at(7),
        reviews_read_at=_at(7),
    )
    route = _serve({FIRST_SINCE: [[]]})
    with _store() as store:
        store.upsert_items([read])
        store.set_sync_state(
            SyncState(
                "acme/a", "items", watermark=_at(7), oldest=_at(3), last_sync_at=_at(7)
            )
        )
        first = await _sync(store, budget=Budget(rest=0))
        assert first.drained is False
        assert [i.number for i in store.items_needing_enrichment("acme/a", 9)] == [1]
        store.mark_enriched([Enrichment("acme/a", 1, None, None, None, None, _at(8))])
        await _sync(store, budget=Budget(rest=0))

        assert route.call_count == 0
        assert store.items_needing_enrichment("acme/a", 9) == []
        state = store.sync_state("acme/a", "items")
        assert (state.watermark, state.oldest) == (
            None,
            datetime(2026, 2, 28, 23, 59, 59, tzinfo=UTC),
        )


@respx.mock
async def test_sync_all_items_keeps_the_scope_spelling_of_a_repository() -> None:
    from github_statistics.client import Transport
    from github_statistics.scope import Repository, ResolvedScope
    from github_statistics.store import SyncState

    route = _serve({"2026-03-05T11:59:59Z": [[]]})
    renamed = Repository("A", "2020-01-01T00:00:00Z", False, False, False, "main")
    scope = ResolvedScope(
        owner="acme", owner_type="Organization", repositories=(renamed,), dropped=()
    )
    with _store() as store:
        store.enter_scope("acme/a", WINDOW)
        oldest = datetime(2026, 2, 28, 23, 59, 59, tzinfo=UTC)
        store.set_sync_state(
            SyncState(
                "acme/a", "items", watermark=_at(5), oldest=oldest, last_sync_at=_at(5)
            )
        )
        store.add_coverage("acme/a", "issues", WINDOW, date(2026, 3, 5))
        async with Transport(None, sleep=_instant, max_retries=0) as transport:
            results = await _api().sync_all_items(
                transport, store, scope, window_start=WINDOW, now=NOW
            )

        assert route.call_count == 1
        assert list(results) == ["acme/a"]
        assert store.sync_state("acme/A", "items") is None


@respx.mock
async def test_pull_requests_issues_bots_and_ghosts_are_stored_as_listed() -> None:
    _serve(
        {
            FIRST_SINCE: [
                [
                    _listed(1, _at(4), closed=_at(4)),
                    _listed(2, _at(5), pr=True, merged=_at(5), closed=_at(5)),
                    _listed(
                        3, _at(6), pr=True, user={"login": "x[bot]", "type": "Bot"}
                    ),
                    _listed(4, _at(7), user=None),
                ]
            ]
        }
    )
    with _store() as store:
        await _sync(store)
        items = {i.number: i for i in store.items_between(WINDOW, date(2026, 3, 9))}

    issue, merged, bot, ghost = items[1], items[2], items[3], items[4]
    assert (issue.kind, issue.node_id, issue.merged_at) == ("issue", "I_1", None)
    assert issue.closed_at == _at(4)
    assert (merged.kind, merged.node_id, merged.merged_at) == ("pr", "PR_2", _at(5))
    assert (bot.author, bot.author_type, bot.merged_at) == ("x[bot]", "Bot", None)
    assert (ghost.author, ghost.author_type) == ("ghost", None)


@respx.mock
async def test_upsert_reopens_an_item_and_keeps_its_enrichment() -> None:
    from github_statistics.store import Item

    _serve({FIRST_SINCE: [[_listed(1, _at(6), pr=True)]]})
    with _store() as store:
        store.upsert_items(
            [
                Item(
                    repo="acme/a",
                    number=1,
                    node_id="PR_1",
                    kind="pr",
                    created_at=_at(1),
                    closed_at=_at(3),
                    merged_at=None,
                    author="alice",
                    author_type="User",
                    updated_at=_at(3),
                    closed_by="bob",
                    closed_by_type="User",
                    reviews_read_at=_at(3),
                )
            ]
        )
        await _sync(store)
        (item,) = store.items_between(WINDOW, date(2026, 3, 9))

    assert item.closed_at is None
    assert item.updated_at == _at(6)
    assert (item.closed_by, item.closed_by_type) == ("bob", "User")
    assert item.reviews_read_at == _at(3)


@respx.mock
async def test_stored_rows_hold_no_title_body_or_author_association() -> None:
    private = {"title": "t", "body": "b", "author_association": "MEMBER"}
    _serve({FIRST_SINCE: [[_listed(1, _at(4)) | private]]})
    with _store() as store:
        await _sync(store)
        row = store.connection.execute("SELECT * FROM items").fetchone()

    assert not set(row.keys()) & {"title", "body", "labels", "author_association"}
    assert not set(private.values()) & set(row)


@pytest.mark.parametrize("status", [403, 404, 410, 502])
@respx.mock
async def test_an_unreadable_repository_changes_no_coverage(status: int) -> None:
    respx.get(LISTING).respond(status, json={"message": "nope"})
    with _store() as store:
        result = await _sync(store)

        assert result.unreadable == status
        assert result.drained is False
        assert store.sync_state("acme/a", "items") is None
        assert _coverage(store, "issues") == []


@respx.mock
async def test_sync_all_items_runs_watermarked_repositories_first() -> None:
    from github_statistics.client import Transport
    from github_statistics.scope import Repository, ResolvedScope
    from github_statistics.store import SyncState

    def repository(name: str) -> Repository:
        return Repository(name, "2020-01-01T00:00:00Z", False, False, False, "main")

    respx.get(f"{API}/repos/acme/gone/issues").respond(404)
    respx.get(f"{API}/repos/acme/new/issues").respond(200, json=[])
    respx.get(f"{API}/repos/acme/old/issues").respond(200, json=[])
    scope = ResolvedScope(
        owner="acme",
        owner_type="Organization",
        repositories=tuple(repository(n) for n in ("gone", "new", "old")),
        dropped=(),
    )
    with _store() as store:
        store.set_sync_state(SyncState("acme/old", "items", watermark=_at(5)))
        async with Transport(
            None, sleep=_instant, max_retries=0, concurrency=1
        ) as transport:
            results = await _api().sync_all_items(
                transport, store, scope, window_start=WINDOW, now=NOW
            )

    order = [call.request.url.path.split("/")[3] for call in respx.calls]
    assert order[0] == "old"
    assert set(order) == {"gone", "new", "old"}
    assert results["acme/gone"].unreadable == 404
    assert results["acme/new"].drained is True
    assert results["acme/old"].drained is True


@pytest.mark.parametrize(
    ("status", "headers"), [(429, {}), (403, {"x-ratelimit-remaining": "0"})]
)
@respx.mock
async def test_an_unbudgeted_rate_limit_stops_one_repository(
    status: int, headers: dict[str, str]
) -> None:
    from github_statistics.client import Transport
    from github_statistics.scope import Repository, ResolvedScope

    following = f"{LISTING}?page=2"
    respx.get(LISTING, params={"page": "2"}).respond(status, headers=headers)
    respx.get(LISTING).respond(
        200, json=[_listed(1, _at(3))], headers={"link": f'<{following}>; rel="next"'}
    )
    respx.get(f"{API}/repos/acme/b/issues").respond(200, json=[])
    scope = ResolvedScope(
        owner="acme",
        owner_type="Organization",
        repositories=tuple(
            Repository(n, "2020-01-01T00:00:00Z", False, False, False, "main")
            for n in ("a", "b")
        ),
        dropped=(),
    )
    with _store() as store:
        async with Transport(
            None, sleep=_instant, max_retries=0, concurrency=1
        ) as transport:
            results = await _api().sync_all_items(
                transport, store, scope, window_start=WINDOW, now=NOW
            )

        assert set(results) == {"acme/a", "acme/b"}
        assert results["acme/a"].drained is False
        assert results["acme/a"].unreadable is None
        assert _watermark(store) == _at(3)
        assert _coverage(store, "issues") == []


@respx.mock
async def test_sync_all_items_raises_an_auth_error_unwrapped() -> None:
    from github_statistics.client import AuthError, Transport
    from github_statistics.scope import Repository, ResolvedScope

    respx.get(f"{API}/repos/acme/a/issues").respond(401)
    respx.get(f"{API}/repos/acme/b/issues").respond(200, json=[])
    scope = ResolvedScope(
        owner="acme",
        owner_type="Organization",
        repositories=tuple(
            Repository(n, "2020-01-01T00:00:00Z", False, False, False, "main")
            for n in ("a", "b")
        ),
        dropped=(),
    )
    with _store() as store:
        async with Transport(None, sleep=_instant, max_retries=0) as transport:
            with pytest.raises(AuthError):
                await _api().sync_all_items(
                    transport, store, scope, window_start=WINDOW, now=NOW
                )


@respx.mock
async def test_a_rejected_token_raises_even_with_the_budget_spent() -> None:
    from github_statistics.client import AuthError, Budget, Transport

    respx.get(f"{API}/user").respond(401)
    listing = respx.get(LISTING).respond(200, json=[])
    with _store() as store:
        async with Transport(
            None, sleep=_instant, max_retries=0, budget=Budget(rest=1)
        ) as transport:
            with pytest.raises(AuthError):
                await transport.request("GET", "/user")
            assert transport.rest_exhausted

            with pytest.raises(AuthError):
                await _api().sync_items(
                    transport, store, "acme", "a", window_start=WINDOW, now=NOW
                )

        assert listing.call_count == 0
        assert _watermark(store) is None
