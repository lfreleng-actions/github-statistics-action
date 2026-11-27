# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Enrich stored items with reviews, mergers and closers (design §6.3).

One GraphQL ``nodes(ids:)`` query reads fifty items, pull requests and
issues together. The items of a batch needing no further review page
are stored in one transaction, with the reviews of every pull request
among them and the merger, closer and read time of every item. A pull
request with more reviews is put aside until every batch is read, then
paged and stored on its own, the fewest reviews first.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from github_statistics.client import (
    BudgetExhaustedError,
    GraphQLError,
    Transport,
    TransportError,
    batched,
    graphql,
)
from github_statistics.client.graphql import TIMEOUT_STATUSES
from github_statistics.store import Enrichment, Item, Review, Store

from .common import last_point

log = logging.getLogger(__name__)

BATCH_SIZE = 50
# A bound on a batch's cost: GitHub charged one point for this query.
BATCH_COST = 2
# GitHub's minimum, what a query reading one item or one page costs.
PAGE_COST = 1
# More items than one repository holds, so a run sees them all.
PER_REPOSITORY = 1_000_000
GHOST = "ghost"
GHOST_TYPE = "User"
# A read node: its id, the rows it marks, the row owning it and its reviews.
_Read = tuple[str, list[Enrichment], Item | None, list[Any] | None]
# A pull request put aside to page: its id, marks, owner and first page.
_Aside = tuple[str, list[Enrichment], Item, dict[str, Any]]

_ACTOR = "login __typename"
_REVIEW = f"id fullDatabaseId author {{ {_ACTOR} }} state submittedAt"
_PLACE = "number repository { nameWithOwner }"
_CLOSER = (
    "timelineItems(itemTypes: [CLOSED_EVENT], last: 1) {"
    f" nodes {{ ... on ClosedEvent {{ actor {{ {_ACTOR} }} }} }} }}"
)

ENRICH_QUERY = f"""
query($ids: [ID!]!) {{
  rateLimit {{ cost remaining }}
  nodes(ids: $ids) {{
    __typename
    ... on PullRequest {{
      id
      {_PLACE}
      mergedBy {{ {_ACTOR} }}
      reviews(first: 50) {{
        totalCount
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ {_REVIEW} }}
      }}
      {_CLOSER}
    }}
    ... on Issue {{
      id
      {_PLACE}
      {_CLOSER}
    }}
  }}
}}
"""

REVIEWS_QUERY = f"""
query($id: ID!, $cursor: String) {{
  rateLimit {{ cost remaining }}
  node(id: $id) {{
    ... on PullRequest {{
      reviews(first: 100, after: $cursor) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ {_REVIEW} }}
      }}
    }}
  }}
}}
"""


@dataclass(frozen=True)
class EnrichResult:
    """What one enrichment run did.

    ``items`` counts the items marked enriched, ``reviews`` the reviews
    stored for them. ``failed`` holds the node ids that failed alone and
    ``remaining`` those not reached when the budget ran out; both are
    read again on the next run.
    """

    items: int
    reviews: int
    failed: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()


def _actor(data: Any) -> tuple[str, str | None]:
    """The login and ``__typename`` of an actor; ``ghost`` for a deleted one."""
    if not isinstance(data, dict):
        return GHOST, GHOST_TYPE
    return data["login"], data.get("__typename")


def _closer(node: dict[str, Any]) -> tuple[str | None, str | None]:
    """The actor of a node's last ``CLOSED_EVENT``; ``None`` when GitHub has none."""
    events = (node.get("timelineItems") or {}).get("nodes") or []
    if not events or not isinstance(events[-1], dict):
        return None, None
    return _actor(events[-1].get("actor"))


def _review(item: Item, data: dict[str, Any]) -> Review | None:
    """The stored record of one review, or ``None`` for a pending one."""
    if data.get("state") == "PENDING":
        return None
    reviewer, reviewer_type = _actor(data.get("author"))
    submitted = data.get("submittedAt")
    return Review(
        id=int(data["fullDatabaseId"]),
        node_id=data["id"],
        repo=item.repo,
        number=item.number,
        reviewer=reviewer,
        reviewer_type=reviewer_type,
        state=data["state"],
        submitted_at=None if submitted is None else datetime.fromisoformat(submitted),
    )


def _owner(rows: list[Item], node: dict[str, Any] | None) -> Item | None:
    """The row stored where GitHub places ``node``, or ``None`` when none is."""
    if node is None:
        return None
    repo = (node.get("repository") or {}).get("nameWithOwner")
    if not isinstance(repo, str):
        return None
    for row in rows:
        if row.number == node.get("number") and row.repo.lower() == repo.lower():
            return row
    return None


def _enrichment(item: Item, node: dict[str, Any] | None) -> Enrichment:
    """What ``node`` adds to ``item``; a missing node keeps what is stored."""
    merged_by = merged_type = closed_by = closed_type = None
    if node is None:
        merged_by, merged_type = item.merged_by, item.merged_by_type
        closed_by, closed_type = item.closed_by, item.closed_by_type
    elif item.merged_at is not None:
        merged_by, merged_type = _actor(node.get("mergedBy"))
        closed_by, closed_type = merged_by, merged_type
    elif item.closed_at is not None:
        closed_by, closed_type = _closer(node)
    return Enrichment(
        repo=item.repo,
        number=item.number,
        merged_by=merged_by,
        merged_by_type=merged_type,
        closed_by=closed_by,
        closed_by_type=closed_type,
        read_at=item.updated_at,
    )


async def _all_reviews(
    transport: Transport, node_id: str, reviews: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every review of one pull request, paging past the batch's first fifty."""
    nodes = list(reviews.get("nodes") or [])
    page = reviews.get("pageInfo") or {}
    while page.get("hasNextPage"):
        data = await graphql(
            transport,
            REVIEWS_QUERY,
            {"id": node_id, "cursor": page.get("endCursor")},
            cost_estimate=PAGE_COST,
        )
        more = (data.get("node") or {}).get("reviews")
        if not isinstance(more, dict):
            gone = {"type": "NOT_FOUND", "message": f"no review page for {node_id}"}
            raise GraphQLError(gone["message"], errors=[gone])
        nodes.extend(more.get("nodes") or [])
        page = more.get("pageInfo") or {}
    return nodes


async def _page(
    transport: Transport,
    aside: Iterable[_Aside],
    save: Callable[[list[_Read]], None],
    failed: list[str],
) -> None:
    """Page and store the pull requests put aside, fewest reviews first.

    A pull request GitHub no longer finds goes in ``failed``. A spent
    budget, or any other failure of a page, such as a ``502``/``504``,
    a timeout or a transport error, logs the pull request it stopped at
    and ends the paging: GitHub is failing, not the item.
    """
    for node_id, marks, owner, reviews in sorted(
        aside, key=lambda a: a[3].get("totalCount") or 0
    ):
        try:
            raw = await _all_reviews(transport, node_id, reviews)
        except BudgetExhaustedError:
            budget = transport.budget
            timed = budget is not None and budget.stop_reason("graphql") == "time"
            log.log(
                logging.INFO if timed else logging.WARNING,
                "%s paging the reviews of %s#%d; it is read again next run",
                "the time limit was reached" if timed else "GraphQL budget ran out",
                owner.repo,
                owner.number,
            )
            return
        except GraphQLError as exc:
            # A request-wide error stops the run, as it does batched().
            if exc.request_wide or exc.status not in (None, *TIMEOUT_STATUSES):
                raise
            if not _not_found(exc, None):
                _stop(owner, exc)
                return
            log.warning("could not page the reviews of %s: %s", node_id, exc)
            failed.append(node_id)
            continue
        except TransportError as exc:
            _stop(owner, exc)
            return
        save([(node_id, marks, owner, raw)])


def _stop(owner: Item, exc: Exception) -> None:
    """Log the failure that ends the paging of the pull requests put aside."""
    log.warning(
        "stopped paging reviews at %s#%d: %s; it and those after it"
        " are read again next run",
        owner.repo,
        owner.number,
        exc,
    )


def _not_found(exc: GraphQLError, path: list[Any] | None) -> bool:
    """Whether every error in ``exc`` says the node queried does not exist.

    With ``path``, every error must also name that path.
    """
    return bool(exc.errors) and all(
        isinstance(error, dict)
        and error.get("type") == "NOT_FOUND"
        and (path is None or error.get("path") == path)
        for error in exc.errors
    )


async def _read(transport: Transport, ids: list[str]) -> list[Any]:
    """The nodes of ``ids``, in order; ``[None]`` for one id GitHub cannot find.

    GitHub answers an id it cannot find with a ``null`` node and a
    ``NOT_FOUND`` error, which fails the whole batch; once halving has
    left that id alone, it reads as a missing node.
    """
    estimate = BATCH_COST if len(ids) > 1 else PAGE_COST
    try:
        data = await graphql(
            transport, ENRICH_QUERY, {"ids": ids}, cost_estimate=estimate
        )
    except GraphQLError as exc:
        if len(ids) == 1 and _not_found(exc, ["nodes", 0]):
            return [None]
        raise
    return list(data.get("nodes") or [None] * len(ids))


async def enrich_items(
    transport: Transport, store: Store, repos: Iterable[str]
) -> EnrichResult:
    """Read reviews, mergers and closers of the items of ``repos`` needing them.

    ``repos`` are the store's ``owner/name`` keys. Items never read, or
    updated since ``reviews_read_at``, go fifty to a query, the most
    recently updated first, so recent days complete first. A pull
    request's reviews replace those stored, pending ones skipped. One
    with more than fifty is put aside until every batch is read, then
    paged and stored on its own, the fewest reviews first; one whose
    review page does not come back fails and keeps its stored reviews.
    Any other failure of a page, such as a ``502``/``504``, a timeout or
    a transport error, ends the paging, leaving that pull request and
    every one not yet paged in ``remaining``.
    So a pull request with a long review history can delay only itself
    and longer ones; it never holds back an item that fits in one page.
    A merged pull request's closer is its merger; a closed item GitHub
    names no closer for keeps ``closed_by`` null. A deleted account
    reads as ``ghost``. ``reviews_read_at`` becomes the ``updated_at``
    the item had when read.

    Rows sharing one node id are read once. Only the row stored at the
    repository and number GitHub reports for the node takes it and its
    reviews; any other row, and the row of a node GitHub no longer
    returns, is marked read with its stored fields kept, so it is not
    retried every run and never takes another row's reviews.

    Run it after the listing sync of ``repos``, never alongside it
    (design §6.5): a listing page stored while a read is in flight can
    lose its same-second invalidation of that item (design §6.3).

    There is no ``budget`` argument: the budget is the one on
    ``transport`` (design §6.5), charged the cost each query reports.
    When fewer points are left than a batch may cost, one item is read
    alone, at GitHub's minimum of one point. When the budget runs out,
    enrichment stops cleanly with the ids it did not reach in
    ``remaining``. A pull request whose review paging the budget cuts
    off keeps its stored reviews, is logged and stays in ``remaining``
    with every pull request put aside after it: one whose pages exceed
    a whole run's budget is never completed. A budget smaller than the
    first pages queued can complete no item, an accepted limit that a
    warning naming ``budget.graphql_points`` reports; a time limit
    reached first is logged at ``INFO`` instead. An id that fails
    alone is logged and left in ``failed`` for the next run.
    """
    items = [
        item
        for repo in repos
        for item in store.items_needing_enrichment(repo, PER_REPOSITORY)
    ]
    items.sort(key=lambda item: item.updated_at, reverse=True)
    # A node can be stored under two keys: an issue transferred between
    # repositories keeps its node id.
    rows: dict[str, list[Item]] = {}
    for item in items:
        rows.setdefault(item.node_id, []).append(item)
    done: set[str] = set()
    aside: dict[str, _Aside] = {}
    stored_items = stored_reviews = 0

    def save(read: list[_Read]) -> None:
        """Store items with their reviews in one transaction and count them."""
        nonlocal stored_items, stored_reviews
        enrichments = [e for _, marks, _, _ in read for e in marks]
        reviews = {
            (owner.repo, owner.number): [
                review for data in raw if (review := _review(owner, data))
            ]
            for _, _, owner, raw in read
            if owner is not None and raw is not None
        }
        store.store_enrichments(enrichments, reviews)
        done.update(node_id for node_id, _, _, _ in read)
        stored_items += len(enrichments)
        stored_reviews += sum(len(r) for r in reviews.values())

    async def fetch(ids: list[str]) -> None:
        """Read one batch, store its items and put aside those to page."""
        # A last point cannot cover a batch but reads one item; the rest
        # of the chunk is then left for the next run.
        budget = transport.budget
        last = len(ids) > 1 and budget is not None and budget.graphql_left < BATCH_COST
        if last:
            ids = ids[:1]
        nodes = await _read(transport, ids)
        ready: list[_Read] = []
        for node_id, node in zip(ids, nodes, strict=True):
            if not isinstance(node, dict) or not node.get("__typename"):
                node = None
            owner = _owner(rows[node_id], node)
            marks = [
                _enrichment(row, None) for row in rows[node_id] if row is not owner
            ]
            if owner is None or node is None:
                ready.append((node_id, marks, None, None))
                continue
            marks.insert(0, _enrichment(owner, node))
            if node["__typename"] != "PullRequest":
                ready.append((node_id, marks, owner, None))
                continue
            reviews = node.get("reviews") or {}
            if (reviews.get("pageInfo") or {}).get("hasNextPage"):
                aside[node_id] = (node_id, marks, owner, reviews)
            else:
                ready.append((node_id, marks, owner, list(reviews.get("nodes") or [])))
        save(ready)
        if last:
            raise last_point(transport, "item")

    def describe(ids: list[str]) -> str:
        """Name a batch in the log."""
        return f"{len(ids)} items from {ids[0]}"

    out = await batched(list(rows), BATCH_SIZE, fetch, describe)
    failed = list(out.failed)
    await _page(transport, aside.values(), save, failed)
    for node_id in failed:
        item = rows[node_id][0]
        log.warning("could not enrich %s#%d (%s)", item.repo, item.number, node_id)
    remaining = tuple(
        node_id
        for node_id in dict.fromkeys([*aside, *out.remaining])
        if node_id not in done and node_id not in failed
    )
    budget = transport.budget
    if remaining and not stored_items and budget and budget.graphql_exhausted:
        if budget.stop_reason("graphql") == "time":
            log.info(
                "the time limit was reached before any item was completed;"
                " %d wait for the next run",
                len(remaining),
            )
        else:
            log.warning(
                "the GraphQL budget was too small to complete any item;"
                " %d wait for the next run: raise budget.graphql_points",
                len(remaining),
            )
    return EnrichResult(
        items=stored_items,
        reviews=stored_reviews,
        failed=tuple(failed),
        remaining=remaining,
    )
