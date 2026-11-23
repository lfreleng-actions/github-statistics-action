# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the workflow run collector (design §6.4)."""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

API = "https://api.github.com"
WINDOW = date(2026, 3, 1)
NOW = datetime(2026, 3, 10, 6, tzinfo=UTC)
YESTERDAY = date(2026, 3, 9)
ALICE = {"login": "alice", "type": "User"}
ETAG = 'W/"listing"'


def _api() -> Any:
    """The collector, loaded when a test runs so a missing one fails that test."""
    return importlib.import_module("github_statistics.collect")


def _at(day: int, hour: int = 12, minute: int = 0, month: int = 3) -> datetime:
    """A UTC timestamp on ``day`` of ``month`` 2026."""
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def _text(value: datetime) -> str:
    """``value`` as GitHub writes a timestamp."""
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _listed(
    run_id: int,
    created: datetime,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    actor: dict[str, str] | None = ALICE,
    triggering: dict[str, str] | None = ALICE,
) -> dict[str, Any]:
    """A workflow run as the listing returns it, with the keys the collector reads."""
    return {
        "id": run_id,
        "node_id": f"WFR_{run_id}",
        "path": ".github/workflows/ci.yaml",
        "event": "push",
        "status": status,
        "conclusion": conclusion,
        "run_attempt": 1,
        "created_at": _text(created),
        "actor": actor,
        "triggering_actor": triggering,
    }


class _Actions:
    """A fake Actions API for ``acme/<repo>`` with the listing's caps.

    The listing serves ``runs`` newest first, 100 a page, stops after
    1,000 items and filters by a ``created=A..B`` range of timestamps.
    The unfiltered first page answers ``304`` to ``etag``.
    """

    def __init__(self, runs: list[dict[str, Any]], repo: str = "a") -> None:
        self.runs = {run["id"]: run for run in runs}
        self.listing = f"{API}/repos/acme/{repo}/actions/runs"
        self.created: list[str] = []
        respx.get(self.listing).mock(side_effect=self._list)
        self.reread = respx.get(url__regex=rf"{self.listing}/\d+$").mock(
            side_effect=self._one
        )

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        page = int(params.get("page", "1"))
        selected = sorted(
            self.runs.values(), key=lambda run: (run["created_at"], run["id"])
        )[::-1]
        created = params.get("created")
        if created is None:
            if page == 1 and request.headers.get("if-none-match") == ETAG:
                return httpx.Response(304)
        else:
            if page == 1:
                self.created.append(created)
            low, high = created.split("..")
            selected = [r for r in selected if low <= r["created_at"] <= high]
        visible = selected[:1000]
        chunk = visible[(page - 1) * 100 : page * 100]
        headers = {"etag": ETAG}
        if page * 100 < len(visible):
            following = request.url.copy_merge_params({"page": page + 1})
            headers["link"] = f'<{following}>; rel="next"'
        body = {"total_count": min(len(selected), 2500), "workflow_runs": chunk}
        return httpx.Response(200, json=body, headers=headers)

    def _one(self, request: httpx.Request) -> httpx.Response:
        run_id = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=self.runs[run_id])


def _store() -> Any:
    """An in-memory store."""
    from github_statistics.store import Store

    return Store(Path(":memory:"))


async def _instant(_: float) -> None:
    return None


async def _sync(
    store: Any, budget: Any = None, repo: str = "a", window: date = WINDOW
) -> Any:
    """Run ``sync_runs`` for ``acme/<repo>`` with ``budget`` on its transport."""
    from github_statistics.client import Transport

    async with Transport(
        None, sleep=_instant, max_retries=0, budget=budget
    ) as transport:
        return await _api().sync_runs(
            transport, store, "acme", repo, window_start=window, now=NOW
        )


def _state(store: Any, repo: str = "acme/a") -> Any:
    return store.sync_state(repo, "runs")


def _seed(store: Any, repo: str = "acme/a", **fields: Any) -> None:
    """Store a runs sync state for ``repo``."""
    from github_statistics.store import SyncState

    store.set_sync_state(SyncState(repo, "runs", **fields))


def _coverage(store: Any, repo: str = "acme/a") -> list[tuple[date, date]]:
    return [(r.from_day, r.to_day) for r in store.coverage(repo, "runs")]


def _ids(store: Any) -> set[int]:
    return {run.id for run in store.runs_between(date(2026, 1, 1), YESTERDAY)}


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_unchanged_listing_answers_304_and_extends_coverage() -> None:
    actions = _Actions([_listed(1, _at(5))])
    with _store() as store:
        _seed(store, watermark=_at(5), etag=ETAG, oldest=_midnight(WINDOW))
        store.add_coverage("acme/a", "runs", WINDOW, date(2026, 3, 5))

        result = await _sync(store)

        assert (result.pages, result.runs, result.current) == (1, 0, True)
        assert result.reached is True
        assert actions.reread.call_count == 0
        assert _coverage(store) == [(WINDOW, YESTERDAY)]
        assert _state(store).last_sync_at == NOW


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_incremental_read_stops_at_the_watermark() -> None:
    old = [_listed(i, _at(2) + timedelta(minutes=i)) for i in range(1, 151)]
    new = [_listed(i, _at(6) + timedelta(minutes=i)) for i in range(151, 301)]
    watermark = _at(2) + timedelta(minutes=150)
    _Actions(old + new)
    with _store() as store:
        _seed(store, watermark=watermark, etag='W/"old"', oldest=_midnight(WINDOW))
        store.add_coverage("acme/a", "runs", WINDOW, date(2026, 3, 2))

        result = await _sync(store)

        assert result.pages == 2 and result.runs == 200
        assert _ids(store) == set(range(101, 301))
        state = _state(store)
        assert state.watermark == _at(6) + timedelta(minutes=300)
        assert state.etag == ETAG
        assert _coverage(store) == [(WINDOW, YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_incremental_read_pages_past_runs_sharing_the_boundary() -> None:
    runs = [_listed(0, _at(9))]
    runs += [_listed(i, _midnight(WINDOW)) for i in range(1, 151)]
    _Actions(runs)
    with _store() as store:
        result = await _sync(store)

        assert result.pages == 2
        assert _ids(store) == set(range(151))
        assert _coverage(store) == [(WINDOW, YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_listing_cap_falls_back_to_a_date_slice() -> None:
    watermark = _at(1)
    runs = [_listed(0, watermark)]
    runs += [_listed(i, _at(3) + timedelta(minutes=i)) for i in range(1, 1101)]
    actions = _Actions(runs)
    with _store() as store:
        _seed(store, watermark=watermark, oldest=_midnight(WINDOW))

        result = await _sync(store)

        assert result.current is True
        assert actions.created == [f"{_text(watermark)}..{_text(_at(3, 13, 41))}"]
        assert _ids(store) == set(range(1101))
        assert _coverage(store) == [(WINDOW, YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_first_sync_of_a_busy_repository_backfills_below_the_cap() -> None:
    runs = [_listed(i, _at(2) + timedelta(minutes=3 * i)) for i in range(1, 1201)]
    actions = _Actions(runs)
    with _store() as store:
        result = await _sync(store)

        assert actions.created == ["2026-03-01T00:00:00Z..2026-03-02T22:03:00Z"]
        assert _ids(store) == set(range(1, 1201))
        assert result.reached is True
        assert _state(store).oldest == _midnight(WINDOW)
        assert _coverage(store) == [(WINDOW, YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_slices_halve_to_a_day_and_split_the_day_by_hours() -> None:
    runs = [_listed(0, _at(9))]
    runs += [_listed(i, _at(5, 0) + timedelta(minutes=i)) for i in range(1, 1051)]
    actions = _Actions(runs)
    with _store() as store:
        _seed(store, watermark=_at(9), etag=ETAG, oldest=_midnight(date(2026, 3, 8)))
        store.add_coverage("acme/a", "runs", date(2026, 3, 8), YESTERDAY)

        await _sync(store)

        assert actions.created[:5] == [
            "2026-03-01T00:00:00Z..2026-03-07T23:59:59Z",
            "2026-03-04T00:00:00Z..2026-03-07T23:59:59Z",
            "2026-03-06T00:00:00Z..2026-03-07T23:59:59Z",
            "2026-03-04T00:00:00Z..2026-03-05T23:59:59Z",
            "2026-03-05T00:00:00Z..2026-03-05T23:59:59Z",
        ]
        hours = actions.created[5:29]
        assert hours[0] == "2026-03-05T23:00:00Z..2026-03-05T23:59:59Z"
        assert hours[-1] == "2026-03-05T00:00:00Z..2026-03-05T00:59:59Z"
        assert actions.created[29:] == [
            "2026-03-04T00:00:00Z..2026-03-04T23:59:59Z",
            "2026-03-01T00:00:00Z..2026-03-03T23:59:59Z",
        ]
        assert _ids(store) == set(range(1, 1051))
        assert _coverage(store) == [(WINDOW, YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_an_hour_over_the_cap_leaves_its_day_uncovered() -> None:
    runs = [_listed(0, _at(9))]
    runs += [_listed(i, _at(5, 10) + timedelta(seconds=3 * i)) for i in range(1, 1101)]
    _Actions(runs)
    with _store() as store:
        _seed(store, watermark=_at(9), etag=ETAG, oldest=_midnight(date(2026, 3, 8)))
        store.add_coverage("acme/a", "runs", date(2026, 3, 8), YESTERDAY)

        result = await _sync(store)

        assert len(_ids(store)) == 1000
        assert result.reached is True
        assert _coverage(store) == [
            (WINDOW, date(2026, 3, 4)),
            (date(2026, 3, 6), YESTERDAY),
        ]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_a_day_left_incomplete_stays_uncovered_on_the_next_sync() -> None:
    _Actions([_listed(i, _at(9, 10) + timedelta(seconds=i)) for i in range(1, 2101)])
    with _store() as store:
        await _sync(store)

        assert len(_ids(store)) < 2100
        assert _coverage(store) == [(date(2026, 3, 3), date(2026, 3, 8))]

        again = await _sync(store)

        assert again.pages == 1
        assert _coverage(store) == [(date(2026, 3, 3), date(2026, 3, 8))]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_an_incremental_hour_over_the_cap_covers_the_days_after_it() -> None:
    runs = [_listed(i, _at(4, 10) + timedelta(seconds=i)) for i in range(1, 2101)]
    runs += [_listed(3000 + day, _at(day)) for day in range(6, 9)]
    _Actions(runs)
    with _store() as store:
        _seed(store, watermark=_at(1), oldest=_midnight(WINDOW))
        store.add_coverage("acme/a", "runs", WINDOW, WINDOW)

        await _sync(store)

        assert len(_ids(store)) < 2103
        assert _state(store).watermark == _at(8)
        assert _coverage(store) == [(WINDOW, WINDOW), (date(2026, 3, 5), YESTERDAY)]

        again = await _sync(store)

        assert again.pages == 1
        assert _coverage(store) == [(WINDOW, WINDOW), (date(2026, 3, 5), YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_backfill_leaves_a_day_an_incremental_hour_left_incomplete() -> None:
    runs = [_listed(0, _at(3))]
    runs += [_listed(i, _at(5, 20) + timedelta(seconds=3)) for i in range(1, 1101)]
    _Actions(runs)
    with _store() as store:
        _seed(store, watermark=_at(5, 9), oldest=_at(5, 8) + timedelta(seconds=1))

        result = await _sync(store)

        assert result.reached is True
        assert 0 in _ids(store)
        assert _coverage(store) == [
            (WINDOW, date(2026, 3, 4)),
            (date(2026, 3, 6), YESTERDAY),
        ]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_spent_budget_keeps_complete_slices_and_resumes_from_oldest() -> None:
    from github_statistics.client import Budget

    window = date(2026, 2, 1)
    runs = [_listed(0, _at(9))]
    runs += [_listed(1, _at(2)), _listed(2, _at(23, month=2))]
    runs += [_listed(3, _at(16, month=2))]
    actions = _Actions(runs)
    with _store() as store:
        _seed(store, watermark=_at(9), etag=ETAG, oldest=_midnight(date(2026, 3, 8)))
        store.add_coverage("acme/a", "runs", date(2026, 3, 8), YESTERDAY)

        cut = await _sync(store, budget=Budget(rest=3), window=window)

        assert (cut.current, cut.reached) == (True, False)
        assert actions.created == [
            "2026-03-01T00:00:00Z..2026-03-07T23:59:59Z",
            "2026-02-22T00:00:00Z..2026-02-28T23:59:59Z",
        ]
        assert _state(store).oldest == _midnight(date(2026, 2, 22))
        assert _coverage(store) == [(date(2026, 2, 22), YESTERDAY)]

        resumed = await _sync(store, window=window)

        assert resumed.reached is True
        assert actions.created[2] == "2026-02-15T00:00:00Z..2026-02-21T23:59:59Z"
        assert _ids(store) == {1, 2, 3}
        assert _coverage(store) == [(date(2026, 2, 15), YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_spent_budget_keeps_each_whole_day_part_of_a_split_slice() -> None:
    from github_statistics.client import Budget

    runs = [_listed(0, _at(9))]
    runs += [_listed(i, _at(5) + timedelta(minutes=i)) for i in range(1, 51)]
    runs += [_listed(i, _at(2, 0) + timedelta(seconds=80 * i)) for i in range(51, 1052)]
    actions = _Actions(runs)
    with _store() as store:
        _seed(store, watermark=_at(9), etag=ETAG, oldest=_midnight(date(2026, 3, 8)))
        store.add_coverage("acme/a", "runs", date(2026, 3, 8), YESTERDAY)

        await _sync(store, budget=Budget(rest=4))

        assert len(actions.created) == 3
        assert _state(store).oldest == _midnight(date(2026, 3, 4))
        assert _coverage(store) == [(date(2026, 3, 4), YESTERDAY)]

        resumed = await _sync(store)

        assert resumed.reached is True
        assert actions.created[3] == "2026-03-01T00:00:00Z..2026-03-03T23:59:59Z"
        assert _ids(store) == set(range(1, 1052))
        assert _coverage(store) == [(WINDOW, YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_spent_budget_after_an_empty_slice_resumes_below_it() -> None:
    from github_statistics.client import Budget

    window = date(2026, 2, 1)
    actions = _Actions([_listed(0, _at(9))])
    with _store() as store:
        _seed(store, watermark=_at(9), etag=ETAG, oldest=_midnight(date(2026, 3, 8)))
        store.add_coverage("acme/a", "runs", date(2026, 3, 8), YESTERDAY)

        cut = await _sync(store, budget=Budget(rest=2), window=window)
        resumed = await _sync(store, budget=Budget(rest=2), window=window)

        assert (cut.reached, resumed.reached) == (False, True)
        assert actions.created == [
            "2026-03-01T00:00:00Z..2026-03-07T23:59:59Z",
            "2026-02-22T00:00:00Z..2026-02-28T23:59:59Z",
        ]
        assert _state(store).oldest == _midnight(window)
        assert _coverage(store) == [(date(2026, 3, 8), YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_spent_budget_keeps_each_hour_of_a_split_day() -> None:
    from github_statistics.client import Budget

    runs = [_listed(0, _at(9))]
    runs += [_listed(i, _at(7, 0) + timedelta(seconds=60 * i)) for i in range(1, 1101)]
    actions = _Actions(runs)
    with _store() as store:
        _seed(store, watermark=_at(9), etag=ETAG, oldest=_midnight(date(2026, 3, 8)))
        store.add_coverage("acme/a", "runs", date(2026, 3, 8), YESTERDAY)

        await _sync(store, budget=Budget(rest=13))

        assert actions.created[-1] == "2026-03-07T16:00:00Z..2026-03-07T16:59:59Z"
        assert _state(store).oldest == _at(7, 16)
        assert _coverage(store) == [(date(2026, 3, 8), YESTERDAY)]
        cut = len(actions.created)

        resumed = await _sync(store)

        assert resumed.reached is True
        assert actions.created[cut:] == ["2026-03-01T00:00:00Z..2026-03-07T15:59:59Z"]
        assert _ids(store) == set(range(1, 1101))
        assert _coverage(store) == [(WINDOW, YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_a_resumed_day_is_covered_when_the_backfill_reaches_the_horizon() -> None:
    from github_statistics.client import Budget

    window = date(2026, 2, 1)
    runs = [_listed(0, _at(9))]
    runs += [_listed(i, _at(7, 13) + timedelta(seconds=36 * i)) for i in range(1, 1100)]
    actions = _Actions(runs)
    with _store() as store:
        _seed(store, watermark=_at(9), etag=ETAG, oldest=_midnight(date(2026, 3, 8)))
        store.add_coverage("acme/a", "runs", date(2026, 3, 8), YESTERDAY)

        await _sync(store, budget=Budget(rest=16), window=window)

        assert _state(store).oldest == _at(7, 13)
        assert _coverage(store) == [(date(2026, 3, 8), YESTERDAY)]
        cut = len(actions.created)

        resumed = await _sync(store, window=window)

        assert resumed.reached is True
        assert actions.created[cut:] == [
            "2026-03-01T00:00:00Z..2026-03-07T12:59:59Z",
            "2026-02-22T00:00:00Z..2026-02-28T23:59:59Z",
        ]
        assert _coverage(store) == [(date(2026, 3, 7), YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_an_hour_over_the_cap_keeps_its_day_uncovered_after_a_resume() -> None:
    from github_statistics.client import Budget

    runs = [_listed(0, _at(9)), _listed(1, _at(7, 3))]
    runs += [_listed(i, _at(7, 20) + timedelta(seconds=3)) for i in range(2, 1102)]
    actions = _Actions(runs)
    with _store() as store:
        _seed(store, watermark=_at(9), etag=ETAG, oldest=_midnight(date(2026, 3, 8)))
        store.add_coverage("acme/a", "runs", date(2026, 3, 8), YESTERDAY)

        await _sync(store, budget=Budget(rest=19))

        assert _state(store).oldest == _at(7, 19)

        resumed = await _sync(store)

        assert resumed.reached is True
        assert actions.created[-1] == "2026-03-01T00:00:00Z..2026-03-07T18:59:59Z"
        assert _coverage(store) == [
            (WINDOW, date(2026, 3, 6)),
            (date(2026, 3, 8), YESTERDAY),
        ]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_spent_budget_on_a_first_sync_keeps_the_pages_read() -> None:
    from github_statistics.client import Budget

    runs = [_listed(i, _at(5) + timedelta(minutes=i)) for i in range(1, 251)]
    actions = _Actions(runs)
    with _store() as store:
        cut = await _sync(store, budget=Budget(rest=2))

        assert (cut.pages, cut.runs, cut.reached) == (2, 200, False)
        assert _ids(store) == set(range(51, 251))
        state = _state(store)
        assert state.watermark == _at(5) + timedelta(minutes=250)
        assert state.oldest == _at(5) + timedelta(minutes=51, seconds=1)
        assert _coverage(store) == [(date(2026, 3, 6), YESTERDAY)]

        resumed = await _sync(store)

        assert resumed.reached is True
        assert actions.created == ["2026-03-01T00:00:00Z..2026-03-05T12:51:00Z"]
        assert _ids(store) == set(range(1, 251))
        assert _coverage(store) == [(WINDOW, YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_spent_budget_while_paging_past_a_watermark_resumes_below() -> None:
    from github_statistics.client import Budget

    runs = [_listed(i, _at(5) + timedelta(minutes=i)) for i in range(1, 251)]
    actions = _Actions(runs)
    with _store() as store:
        _seed(store, watermark=_at(1), oldest=_midnight(WINDOW))
        store.add_coverage("acme/a", "runs", WINDOW, WINDOW)

        cut = await _sync(store, budget=Budget(rest=2))

        assert (cut.current, cut.runs) == (False, 200)
        assert _ids(store) == set(range(51, 251))
        assert _state(store).watermark == _at(5) + timedelta(minutes=250)
        assert _coverage(store) == [(WINDOW, WINDOW)]

        resumed = await _sync(store, budget=Budget(rest=2))

        assert (resumed.current, resumed.pages) == (True, 2)
        assert actions.created == ["2026-03-01T12:00:00Z..2026-03-05T12:51:00Z"]
        assert _ids(store) == set(range(1, 251))
        assert _coverage(store) == [(WINDOW, YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_spent_budget_after_filling_a_hole_keeps_its_start() -> None:
    from github_statistics.client import Budget

    runs = [_listed(i, _at(5) + timedelta(minutes=i)) for i in range(1, 251)]
    actions = _Actions(runs)
    with _store() as store:
        _seed(store, watermark=_at(1), oldest=_midnight(WINDOW))
        store.add_coverage("acme/a", "runs", WINDOW, WINDOW)
        await _sync(store, budget=Budget(rest=2))

        filled = await _sync(store, budget=Budget(rest=1))

        assert (filled.current, filled.pages) == (False, 1)
        assert _ids(store) == set(range(1, 251))
        assert _coverage(store) == [(WINDOW, WINDOW)]

        resumed = await _sync(store)

        assert resumed.current is True
        assert actions.created == ["2026-03-01T12:00:00Z..2026-03-05T12:51:00Z"]
        assert _coverage(store) == [(WINDOW, YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_spent_budget_between_pages_of_a_slice_resumes_below_them() -> None:
    from github_statistics.client import Budget

    runs = [_listed(0, _at(9))]
    runs += [_listed(i, _at(2, 0) + timedelta(minutes=5 * i)) for i in range(1, 351)]
    actions = _Actions(runs)
    with _store() as store:
        _seed(store, watermark=_at(9), etag=ETAG, oldest=_midnight(date(2026, 3, 8)))
        store.add_coverage("acme/a", "runs", date(2026, 3, 8), YESTERDAY)

        await _sync(store, budget=Budget(rest=3))

        assert _state(store).oldest == _at(2, 12, 35) + timedelta(seconds=1)
        assert _coverage(store) == [(date(2026, 3, 3), YESTERDAY)]

        resumed = await _sync(store, budget=Budget(rest=3))

        assert resumed.reached is True
        assert actions.created[-1] == "2026-03-01T00:00:00Z..2026-03-02T12:35:00Z"
        assert _ids(store) == set(range(1, 351))
        assert _coverage(store) == [(WINDOW, YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_empty_slices_above_a_held_run_do_not_end_the_backfill() -> None:
    from github_statistics.store import Run

    window = date(2026, 2, 1)
    held = _at(10, month=2)
    actions = _Actions([_listed(0, _at(9)), _listed(1, held)])
    with _store() as store:
        store.upsert_runs(
            [
                Run(
                    id=1,
                    node_id="WFR_1",
                    repo="acme/a",
                    workflow_path=".github/workflows/ci.yaml",
                    event="push",
                    status="completed",
                    conclusion="success",
                    created_at=held,
                    actor="alice",
                    actor_type="User",
                    attempt=1,
                )
            ]
        )
        _seed(store, watermark=_at(9), etag=ETAG, oldest=_midnight(date(2026, 3, 8)))
        store.add_coverage("acme/a", "runs", date(2026, 3, 8), YESTERDAY)

        result = await _sync(store, window=window)

        assert result.reached is True
        assert actions.created == [
            "2026-03-01T00:00:00Z..2026-03-07T23:59:59Z",
            "2026-02-22T00:00:00Z..2026-02-28T23:59:59Z",
            "2026-02-15T00:00:00Z..2026-02-21T23:59:59Z",
            "2026-02-08T00:00:00Z..2026-02-14T23:59:59Z",
            "2026-02-01T00:00:00Z..2026-02-07T23:59:59Z",
        ]
        assert _coverage(store) == [(date(2026, 2, 8), YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_backfill_ends_at_the_listing_horizon() -> None:
    window = date(2026, 1, 1)
    runs = [_listed(0, _at(9)), _listed(1, _at(5)), _listed(2, _at(18, month=2))]
    actions = _Actions(runs)
    with _store() as store:
        _seed(store, watermark=_at(9), etag=ETAG, oldest=_midnight(date(2026, 3, 8)))
        store.add_coverage("acme/a", "runs", date(2026, 3, 8), YESTERDAY)

        result = await _sync(store, window=window)

        assert actions.created == [
            "2026-03-01T00:00:00Z..2026-03-07T23:59:59Z",
            "2026-02-22T00:00:00Z..2026-02-28T23:59:59Z",
            "2026-02-15T00:00:00Z..2026-02-21T23:59:59Z",
            "2026-02-08T00:00:00Z..2026-02-14T23:59:59Z",
            "2026-02-01T00:00:00Z..2026-02-07T23:59:59Z",
        ]
        assert result.reached is True
        assert _state(store).oldest == _midnight(window)
        assert _coverage(store) == [(date(2026, 2, 15), YESTERDAY)]
        assert _api().runs_horizon(store, "acme") == date(2026, 2, 18)

        again = await _sync(store, window=window)

        assert again.pages == 1
        assert len(actions.created) == 5


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_pending_runs_are_reread_up_to_fifty() -> None:
    from github_statistics.store import Run

    pending = [
        _listed(i, _at(5) + timedelta(minutes=i), status="queued", conclusion=None)
        for i in range(1, 61)
    ]
    actions = _Actions(
        [dict(run, status="completed", conclusion="success") for run in pending]
    )
    with _store() as store:
        store.upsert_runs(
            Run(
                id=run["id"],
                node_id=run["node_id"],
                repo="acme/a",
                workflow_path=run["path"],
                event="push",
                status="queued",
                conclusion=None,
                created_at=datetime.fromisoformat(run["created_at"]),
                actor="alice",
                actor_type="User",
                attempt=1,
            )
            for run in pending
        )
        _seed(store, watermark=_at(6), etag=ETAG, oldest=_midnight(WINDOW))

        result = await _sync(store)

        assert actions.reread.call_count == 50
        assert result.pages == 51
        assert [run.id for run in store.pending_runs("acme/a")] == list(range(51, 61))


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_unreadable_pending_reread_adds_no_coverage() -> None:
    from github_statistics.store import Run

    running = _listed(1, _at(5), status="in_progress", conclusion=None)
    actions = _Actions([running, _listed(2, _at(8))])
    actions.reread.mock(return_value=httpx.Response(404))
    with _store() as store:
        store.upsert_runs(
            [
                Run(
                    id=1,
                    node_id="WFR_1",
                    repo="acme/a",
                    workflow_path=running["path"],
                    event="push",
                    status="in_progress",
                    conclusion=None,
                    created_at=_at(5),
                    actor="alice",
                    actor_type="User",
                    attempt=1,
                )
            ]
        )
        _seed(store, watermark=_at(5), etag='W/"old"', oldest=_midnight(WINDOW))
        store.add_coverage("acme/a", "runs", WINDOW, date(2026, 3, 4))

        result = await _sync(store)

        assert result.unreadable == 404
        assert _coverage(store) == [(WINDOW, date(2026, 3, 4))]
        state = _state(store)
        assert (state.watermark, state.etag) == (_at(5), 'W/"old"')
        assert _ids(store) == {1, 2}


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_runs_listed_in_progress_wait_for_the_next_sync() -> None:
    actions = _Actions([_listed(1, _at(9), status="in_progress", conclusion=None)])
    with _store() as store:
        await _sync(store)

        assert actions.reread.call_count == 0
        assert [run.id for run in store.pending_runs("acme/a")] == [1]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_actor_comes_from_triggering_actor_falling_back_to_actor() -> None:
    bot = {"login": "dependabot[bot]", "type": "Bot"}
    _Actions(
        [
            _listed(1, _at(4), actor=bot, triggering=ALICE),
            _listed(2, _at(5), actor=bot, triggering=None),
            _listed(3, _at(6), actor=None, triggering=None),
        ]
    )
    with _store() as store:
        await _sync(store)

        runs = store.runs_between(WINDOW, YESTERDAY)
        assert [(r.actor, r.actor_type) for r in runs] == [
            ("alice", "User"),
            ("dependabot[bot]", "Bot"),
            (None, None),
        ]
        assert (runs[0].workflow_path, runs[0].event, runs[0].attempt) == (
            ".github/workflows/ci.yaml",
            "push",
            1,
        )


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_actions_disabled_covers_the_window_with_no_runs() -> None:
    _Actions([])
    with _store() as store:
        result = await _sync(store)

        assert (result.current, result.reached, result.runs) == (True, True, 0)
        assert _coverage(store) == [(WINDOW, YESTERDAY)]


@pytest.mark.xfail(strict=True)
@pytest.mark.parametrize("status", [403, 404])
@respx.mock
async def test_unreadable_repository_keeps_coverage_and_spares_the_others(
    status: int,
) -> None:
    from github_statistics.client import Transport
    from github_statistics.scope import Repository, ResolvedScope

    def repository(name: str) -> Repository:
        return Repository(name, "2020-01-01T00:00:00Z", False, False, False, "main")

    respx.get(f"{API}/repos/acme/gone/actions/runs").respond(status)
    _Actions([_listed(1, _at(5))], repo="ok")
    scope = ResolvedScope(
        owner="acme",
        owner_type="Organization",
        repositories=(repository("gone"), repository("ok")),
        dropped=(),
    )
    with _store() as store:
        store.add_coverage("acme/gone", "runs", WINDOW, date(2026, 3, 4))
        async with Transport(None, sleep=_instant, max_retries=0) as transport:
            results = await _api().sync_all_runs(
                transport, store, scope, window_start=WINDOW, now=NOW
            )

        assert results["acme/gone"].unreadable == status
        assert results["acme/gone"].current is False
        assert _coverage(store, "acme/gone") == [(WINDOW, date(2026, 3, 4))]
        assert _state(store, "acme/gone") is None
        assert results["acme/ok"].unreadable is None
        assert _coverage(store, "acme/ok") == [(WINDOW, YESTERDAY)]


@pytest.mark.xfail(strict=True)
@respx.mock
async def test_every_incremental_read_comes_before_any_backfill() -> None:
    from github_statistics.client import Transport
    from github_statistics.scope import Repository, ResolvedScope

    names = ("a", "b", "c")
    for name in names:
        _Actions([_listed(1, _at(9)), _listed(2, _at(3))], repo=name)
    scope = ResolvedScope(
        owner="acme",
        owner_type="Organization",
        repositories=tuple(
            Repository(n, "2020-01-01T00:00:00Z", False, False, False, "main")
            for n in names
        ),
        dropped=(),
    )
    with _store() as store:
        for name in names:
            _seed(
                store,
                repo=f"acme/{name}",
                watermark=_at(9),
                etag=ETAG,
                oldest=_midnight(date(2026, 3, 8)),
            )
        async with Transport(None, sleep=_instant, max_retries=0) as transport:
            results = await _api().sync_all_runs(
                transport, store, scope, window_start=WINDOW, now=NOW
            )

        sliced = ["created" in call.request.url.params for call in respx.calls]
        assert sliced == [False] * 3 + [True] * 3
        assert all(result.reached for result in results.values())
