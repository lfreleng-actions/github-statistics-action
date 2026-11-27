# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Collect issues and pull requests from the issues listing (design §6.2).

One listing per repository, ordered by ``updated_at``, feeds both the
``issues`` and the ``pull_requests`` metrics. Each page is stored with
the watermark it advances to in one transaction, so an interrupted sync
resumes from a consistent point. Coverage is all-or-nothing per sync:
an item read late in the listing can carry an event from any earlier
day, so only a listing read to its end proves any day complete.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from github_statistics.client import BudgetExhaustedError, Transport, get_paged
from github_statistics.scope import ResolvedScope
from github_statistics.store import Item, Store, SyncState

from .common import run_concurrently, scope_keys, unreadable_status

log = logging.getLogger(__name__)

# The sync state of the listing both metrics share.
ITEMS_METRIC = "items"
ISSUES_METRIC = "issues"
PULL_REQUESTS_METRIC = "pull_requests"
COVERAGE_METRICS = (ISSUES_METRIC, PULL_REQUESTS_METRIC)

GHOST = "ghost"
_OVERLAP = timedelta(seconds=1)
# How long past its last item's second a page must be dated to keep its
# cursor: the host dates a response when it sends it, to the second, after
# the read, on a clock that may drift from the one stamping `updated_at`.
_SETTLE = timedelta(minutes=1)
_REFUSED_CURSOR = (400, 422)


@dataclass(frozen=True)
class ItemSyncResult:
    """What one repository's item sync did.

    ``drained`` is true when the listing was read to its end, which is
    when coverage is added. ``unreadable`` holds the HTTP status that
    made the repository unreadable for this run (a ``403`` that is not
    a rate limit, a ``404``, a ``410``, or a ``5xx`` the transport's
    retries did not clear), and is ``None`` otherwise.
    """

    repo: str
    items: int
    pages: int
    drained: bool
    unreadable: int | None = None


def _next_params(response: httpx.Response) -> httpx.QueryParams | None:
    """The query of the page's ``next`` link, or ``None`` on the last page.

    GitHub's links carry an ``after`` cursor, which keeps the read by key
    while items move to the end of the listing (design §6.2).
    """
    link = response.links.get("next")
    target = link.get("url") if link else None
    return response.url.join(target).params if target else None


def _settled(response: httpx.Response, watermark: datetime, now: datetime) -> bool:
    """Whether ``response`` was sent well after the second of ``watermark``.

    An item read in the second of its ``updated_at`` can change again in
    that second without that key advancing, so a cursor past it would
    skip the change, where a resume one second before the watermark
    rereads it. The host's ``Date`` header, from the clock that stamps
    ``updated_at``, dates the page; ``now``, taken before the run's first
    request, stands in without one. Either must lie ``_SETTLE`` past the
    watermark, a margin for the time between the read and the ``Date``
    and for drift between the host's clocks.
    """
    try:
        read = parsedate_to_datetime(response.headers["date"])
    except (KeyError, ValueError):
        return watermark + _SETTLE <= now
    # RFC 5322 reads "-0000" as no zone; an HTTP date is always UTC.
    return watermark + _SETTLE <= read.replace(tzinfo=read.tzinfo or UTC)


async def _listing(
    transport: Transport,
    store: Store,
    state: SyncState,
    path: str,
    params: dict[str, Any],
) -> AsyncIterator[tuple[list[Any], httpx.Response]]:
    """The pages of ``path`` from ``params``, as `get_paged` yields them.

    A ``400`` or ``422`` answering a first request that carries an
    ``after`` cursor refuses the cursor: it is cleared from the stored
    ``state``, and the listing is requested again from ``since`` alone.
    """
    pages = get_paged(transport, "/" + path, params)
    try:
        first = await anext(pages)
    except httpx.HTTPStatusError as exc:
        if "after" not in params or exc.response.status_code not in _REFUSED_CURSOR:
            raise
        log.info("the host refused the cursor of %s; resuming by time", state.repo)
        store.set_sync_state(dataclasses.replace(state, cursor=None))
        params = {name: value for name, value in params.items() if name != "after"}
        pages = get_paged(transport, "/" + path, params)
        first = await anext(pages)
    yield first
    async for page in pages:
        yield page


def _parse(text: str | None) -> datetime | None:
    """A GitHub timestamp as an aware `datetime`, or ``None``."""
    return None if text is None else datetime.fromisoformat(text)


def _item(repo: str, data: dict[str, Any]) -> Item:
    """The stored record of one listed issue or pull request.

    Reads only the fields the store keeps: never a title, a body, labels
    or ``author_association``.
    """
    pull_request = data.get("pull_request")
    merged_at = None if pull_request is None else pull_request.get("merged_at")
    user = data.get("user")
    return Item(
        repo=repo,
        number=int(data["number"]),
        node_id=data["node_id"],
        kind="issue" if pull_request is None else "pr",
        created_at=datetime.fromisoformat(data["created_at"]),
        closed_at=_parse(data.get("closed_at")),
        merged_at=_parse(merged_at),
        author=GHOST if user is None else user["login"],
        author_type=None if user is None else user.get("type"),
        updated_at=datetime.fromisoformat(data["updated_at"]),
    )


def _start(state: SyncState, window_start: date) -> tuple[SyncState, datetime]:
    """The state to sync from and the ``since`` to list from.

    A scan starts one second before the window start, 00:00 UTC, so an
    item updated at that instant is listed whether ``since`` includes
    it or not, and records that start as ``oldest``: the scan's chain of
    listings, finished or not, has read nothing older. A later sync
    lists from one second before the watermark, unless the window now
    starts before ``oldest``, which a wider window or a prune (which
    raises ``oldest`` to its day) can leave. Those days were never
    listed by this chain, or a prune has since dropped them, so the
    scan starts over from this window. Coverage an earlier chain left
    proves nothing about this one, so it is not consulted. A scan whose
    watermark is older than the window's midnight also starts over,
    rather than rereading items the window no longer needs or restoring
    pruned ones; its new ``oldest`` keeps a later, wider window checked
    against what this scan listed. An unfinished scan the window has
    passed resumes from its watermark instead, so the rows it left
    mixing two reads are read again before a drained listing clears
    its mark. One with no watermark, as a refresh or the version 2
    migration leaves, keeps in ``oldest`` the point its unread items
    lie after, and restarts from there when that is earlier. Starting
    over drops the stored cursor with the watermark.
    """
    midnight = datetime.combine(window_start, time(), tzinfo=UTC)
    start = midnight - _OVERLAP
    if state.unfinished and state.watermark is None and state.oldest is not None:
        start = min(start, state.oldest)
    behind = state.oldest is None or midnight < state.oldest
    stale = (
        state.watermark is not None
        and state.watermark < midnight
        and not state.unfinished
    )
    if state.watermark is None or behind or stale:
        restart = dataclasses.replace(
            state, watermark=None, oldest=start, last_sync_at=None, cursor=None
        )
        return restart, start
    return state, state.watermark - _OVERLAP


def _add_coverage(store: Store, repo: str, window_start: date, now: datetime) -> None:
    """Cover each metric from ``window_start`` to the day before ``now``.

    A drained listing has read every item updated since a scan that
    started at or before the window start, so it proves the whole window.
    """
    last = now.astimezone(UTC).date() - timedelta(days=1)
    if window_start <= last:
        for metric in COVERAGE_METRICS:
            store.add_coverage(repo, metric, window_start, last)


async def sync_items(
    transport: Transport,
    store: Store,
    owner: str,
    repo: str,
    *,
    window_start: date,
    now: datetime,
) -> ItemSyncResult:
    """Sync the issues and pull requests of ``owner/repo`` into ``store``.

    The first sync lists from one second before ``window_start`` at
    00:00 UTC; later ones from one second before the stored watermark,
    unless the window now starts before the days already listed, and
    from the stored ``after`` cursor of a sync that stopped short. A
    cursor the host refuses with a ``400`` or ``422`` is dropped and the
    listing requested again from the watermark alone. A
    restart of a stored sync is saved before the first page; one for a
    wider window also marks the pull requests created before the
    earlier scan's start for review enrichment in the same transaction,
    since a prune may have dropped their older reviews.
    There is no ``budget`` argument: the budget is the one the caller
    configures on ``transport`` (design §6.5), so it cannot disagree
    with the one the transport charges.
    The transport checks its budget before each page, after refusing a
    token GitHub rejected with ``AuthError``; when the budget is spent,
    the transport raises ``BudgetExhaustedError``, or an
    unbudgeted transport returns a rate limit its retries did not clear,
    the sync stops with ``drained=False``. A ``next`` link without an
    ``after`` cursor logs one warning, and the sync carries on. Any other ``403``, a ``404``,
    ``410`` or a ``5xx``
    left after the transport's retries marks the repository unreadable
    with no coverage change. Coverage of both metrics, over the window,
    is added only when the listing is read to its end. Storing a page
    marks the sync unfinished and keeps the cursor of the page after it,
    unless that page, or an earlier one of the run, was dated less than
    a minute past the second of its last item's ``updated_at``, and
    reading to the end clears both, so a sync
    stopped short leaves them while coverage stays as it was.
    """
    key = f"{owner}/{repo}"
    saved = store.sync_state(key, ITEMS_METRIC)
    previous = saved or SyncState(key, ITEMS_METRIC)
    state, since = _start(previous, window_start)
    if saved is not None and state is not previous:
        oldest = saved.oldest
        wider = (
            oldest is not None and state.oldest is not None and state.oldest < oldest
        )
        # A prune may have dropped reviews older than the earlier scan's start.
        store.restart_item_sync(state, oldest if wider else None)
    path = "/".join(quote(part, safe="") for part in ("repos", owner, repo, "issues"))
    params = {
        "state": "all",
        "sort": "updated",
        "direction": "asc",
        "per_page": 100,
        "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if state.cursor is not None:
        params["after"] = state.cursor
    stored = pages = 0
    drained = warned = False
    settled = True
    try:
        async for page, response in _listing(transport, store, state, path, params):
            pages += 1
            following = _next_params(response)
            cursor = None if following is None else following.get("after")
            if not warned and following is not None and cursor is None:
                warned = True
                log.warning(
                    "the host pages the items of %s by position, not by cursor,"
                    " so items updated during the read may be missed",
                    key,
                )
            items = [_item(key, data) for data in page]
            if items:
                watermark = items[-1].updated_at
                # A cursor past an unsettled page, as a later page's would
                # be, skips what a resume by time would reread.
                settled = settled and _settled(response, watermark, now)
                state = dataclasses.replace(
                    state,
                    watermark=watermark,
                    unfinished=True,
                    cursor=cursor if settled else None,
                )
                store.store_item_page(items, state)
                stored += len(items)
        drained = True
    except BudgetExhaustedError as exc:
        log.info("stopped syncing items of %s: %s", key, exc)
    except httpx.HTTPStatusError as exc:
        status = unreadable_status(exc)
        if status is None:
            log.info("stopped syncing items of %s: rate limited", key)
        else:
            log.warning("cannot read items of %s: HTTP %s", key, status)
            return ItemSyncResult(key, stored, pages, drained=False, unreadable=status)
    if drained:
        done = dataclasses.replace(
            state, last_sync_at=now, unfinished=False, cursor=None
        )
        store.set_sync_state(done)
        _add_coverage(store, key, window_start, now)
    return ItemSyncResult(key, stored, pages, drained)


async def sync_all_items(
    transport: Transport,
    store: Store,
    scope: ResolvedScope,
    *,
    window_start: date,
    now: datetime,
) -> dict[str, ItemSyncResult]:
    """Sync every repository in ``scope`` concurrently; results by ``owner/name``.

    The transport's semaphore bounds the concurrency. Repositories with
    a watermark, the quick incremental work, start before those without.
    A repository keeps the spelling the store's scope holds for it, as
    names compare without case, so a rename in case alone keeps its
    watermark, items and coverage under one key. An error that stops a
    repository, ``AuthError`` first, propagates as itself, not wrapped
    in an ``ExceptionGroup``, and cancels the other repositories.
    """

    def fresh(key: str) -> bool:
        """Whether ``key`` has no watermark yet."""
        state = store.sync_state(key, ITEMS_METRIC)
        return state is None or state.watermark is None

    syncs = []
    for key in sorted(scope_keys(store, scope, now), key=fresh):
        owner, _, name = key.partition("/")
        syncs.append(
            sync_items(
                transport, store, owner, name, window_start=window_start, now=now
            )
        )
    return {result.repo: result for result in await run_concurrently(syncs)}
