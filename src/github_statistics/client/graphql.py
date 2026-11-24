# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""GraphQL queries and halving batches over a ``Transport``."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from .errors import BudgetExhaustedError, GraphQLError
from .policy import (
    errors_name_secondary,
    errors_rate_limited,
    rate_limited,
    write_operation,
)
from .transport import Transport

log = logging.getLogger(__name__)

T = TypeVar("T")
K = TypeVar("K")

# How GitHub answers a GraphQL query that ran out of time.
TIMEOUT_STATUSES = frozenset({502, 504})

# Chunks a batch lets time out before it stops: each one may have spent
# the transport's retries and wait, so persistent 502s would otherwise
# outlast the job. Enough for one id's descent from a chunk of 100.
MAX_TIMEOUTS = 10

# Seconds a batch lets its timed-out chunks take, their attempts and
# backoff together, before it stops.
MAX_TIMEOUT_SECONDS = 300.0

# What GitHub says, in an HTTP 200 error payload, of a query that may
# have run out of time.
TIMEOUT_DIAGNOSTIC = "the result of a timeout"

# Error types a smaller query can avoid, though GitHub refuses the query.
SIZE_ERROR_TYPES = frozenset({"MAX_NODE_LIMIT_EXCEEDED", "RESOURCE_LIMITS_EXCEEDED"})


def _request_wide(payload: dict[str, Any], errors: list[Any]) -> bool:
    """Whether an error payload fails the whole query, whatever ids it names.

    A rate-limit error, primary or secondary, is. So is a payload
    without ``data``, which GitHub sends for a query it refused to run
    (a syntax or validation error), unless the refusal is about the
    query's size or its time.
    """
    if errors_rate_limited(errors):
        return True
    types = {error.get("type") for error in errors if isinstance(error, dict)}
    return (
        "data" not in payload
        and not types & SIZE_ERROR_TYPES
        and not _names_timeout(errors)
    )


def _names_timeout(errors: list[Any]) -> bool:
    """Whether an error payload carries GitHub's timeout diagnostic."""
    return any(
        isinstance(error, dict)
        and isinstance(error.get("message"), str)
        and TIMEOUT_DIAGNOSTIC in error["message"].lower()
        for error in errors
    )


def _timed_out(exc: GraphQLError) -> bool:
    """Whether ``exc`` is a query that ran out of time.

    GitHub reports one as a ``502`` or ``504``, or as an HTTP ``200``
    whose error payload says it may be the result of a timeout.
    """
    if exc.status is not None:
        return exc.status in TIMEOUT_STATUSES
    return _names_timeout(exc.errors)


def _record_rate(transport: Transport, data: Any) -> int | None:
    """Record ``data``'s ``rateLimit`` in the stats; return its cost, if any."""
    rate = data.get("rateLimit") if isinstance(data, dict) else None
    if not isinstance(rate, dict):
        return None
    remaining = rate.get("remaining")
    if isinstance(remaining, int):
        transport.stats.graphql_remaining = remaining
    cost = rate.get("cost")
    if not isinstance(cost, int):
        return None
    transport.stats.graphql_cost += cost
    return cost


async def graphql(
    transport: Transport,
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    cost_estimate: int | None = None,
) -> dict[str, Any]:
    """Post the read-only ``query`` and return its ``data``.

    A document defining a ``mutation`` or ``subscription`` raises
    ``ValueError`` before anything is sent: the client only reads. The
    check guards against misuse and is not a security boundary.

    GitHub answers a failed query with HTTP ``200`` and an ``errors``
    payload; that raises ``GraphQLError``, marked ``request_wide`` for a
    rate limit or a query GitHub refused to run, as does any status
    other than ``200``, a redirect included, since GitHub does not
    redirect its GraphQL endpoint. The transport retries a ``5xx``, a
    ``429`` and a rate-limit ``403`` like any other request; a ``502`` or
    ``504`` that outlasts the retries raises, so ``batched`` can split
    the query.

    With a budget, ``cost_estimate`` is required: a bound on the points
    the query may cost, which GitHub does not report until it has run.
    It is taken from the budget once, before the query is sent, and
    ``BudgetExhaustedError`` is raised when it is not left, or given back
    when the query fails before any attempt left the client; the query is
    then charged the cost it reported in ``rateLimit { cost remaining }``,
    even on an error, or the estimate when it reported none.

    A rate limit that outlasts the transport's retries (a ``429``, or a
    ``403`` with ``Retry-After``, ``x-ratelimit-remaining: 0`` or a body
    naming a secondary rate limit) stops its resource for the run. So
    does an HTTP ``200`` payload with a primary rate-limit error
    (``RATE_LIMITED``, or ``RATE_LIMIT`` with code ``graphql_rate_limit``),
    which stops GraphQL, or with an error whose message names a secondary rate
    limit, which stops every resource; later requests on a stopped
    resource raise ``BudgetExhaustedError`` unsent. Budgeted, the first
    such response also stops the GraphQL budget and raises
    ``BudgetExhaustedError``, so the collector stops cleanly; unbudgeted,
    it raises a request-wide ``GraphQLError``. Any other ``403`` stays a
    permission error.
    """
    operation = write_operation(query)
    if operation is not None:
        raise ValueError(f"the client is read-only; refusing a GraphQL {operation}")
    budget = transport.budget
    estimate = 0
    if budget is not None:
        if cost_estimate is None:
            raise ValueError("a budgeted GraphQL query needs a cost estimate")
        estimate = max(cost_estimate, 1)
    response = await transport._post_graphql(
        {"query": query, "variables": variables or {}}, estimate
    )
    cost: int | None = None
    try:
        if response.status_code != 200:
            limited = rate_limited(response)
            failure = GraphQLError(
                f"GraphQL HTTP {response.status_code}",
                status=response.status_code,
                request_wide=limited,
            )
            if limited and budget is not None:
                budget.exhaust_graphql()
                raise BudgetExhaustedError(
                    "GitHub rate-limited a GraphQL query"
                ) from failure
            raise failure
        payload = response.json()
        data = payload.get("data") or {}
        cost = _record_rate(transport, data)
    finally:
        if budget is not None:
            budget.settle_graphql(estimate, cost)
    errors = payload.get("errors")
    if errors:
        first = (
            errors[0].get("message", "unknown error")
            if isinstance(errors[0], dict)
            else errors[0]
        )
        failure = GraphQLError(
            f"GraphQL query failed: {first}",
            errors=list(errors),
            request_wide=_request_wide(payload, errors),
        )
        if errors_rate_limited(errors):
            transport.stop(errors_name_secondary(errors))
            if budget is not None:
                budget.exhaust_graphql()
                raise BudgetExhaustedError(
                    "GitHub rate-limited a GraphQL query"
                ) from failure
        raise failure
    return data


@dataclass
class Batched(Generic[T, K]):
    """What ``batched`` fetched: one result per chunk, and the ids it did not.

    ``failed`` holds the ids that failed alone; ``remaining`` the ids
    never fetched because the budget ran out or too many chunks timed
    out, in their original order.
    """

    results: list[T] = field(default_factory=list)
    failed: list[K] = field(default_factory=list)
    remaining: list[K] = field(default_factory=list)


async def batched(
    ids: Sequence[K],
    size: int,
    fetch: Callable[[list[K]], Awaitable[T]],
    describe: Callable[[list[K]], str],
    max_timeouts: int = MAX_TIMEOUTS,
    max_timeout_seconds: float = MAX_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Batched[T, K]:
    """Call ``fetch`` on chunks of ``ids`` of at most ``size``, halving on failure.

    A chunk that fails with an error payload or a ``502``/``504`` is
    split in two and each half retried, down to one id; an id that fails
    alone goes in ``failed`` and the rest carry on. A request-wide error
    (a rate limit, a query GitHub refused) propagates at once, since
    splitting cannot avoid it and would multiply the failing requests;
    so does any other error. ``describe(chunk)`` names a chunk in the log.

    When ``fetch`` raises ``BudgetExhaustedError``, or the chunks that
    timed out, by a ``502``/``504`` or an error payload carrying GitHub's
    timeout diagnostic, reach ``max_timeouts`` or have taken
    ``max_timeout_seconds`` by ``clock``, requests and retry waits
    included, batching stops: the results so far return, with the chunk
    that met it and every chunk not yet fetched in ``remaining``, so a
    collector keeps its work and persistent timeouts cannot outlast the
    run. Chunks that succeed, however slow, are not bounded.
    """
    if size < 1:
        raise ValueError(f"batch size must be at least 1, not {size}")
    out: Batched[T, K] = Batched()
    pending = [list(ids[start : start + size]) for start in range(0, len(ids), size)]
    pending.reverse()
    timeouts = 0
    timed_out = 0.0
    while pending:
        chunk = pending.pop()
        started = clock()
        try:
            out.results.append(await fetch(chunk))
        except BudgetExhaustedError:
            log.info("budget exhausted at %s", describe(chunk))
            out.remaining = chunk + [i for rest in reversed(pending) for i in rest]
            return out
        except GraphQLError as exc:
            if exc.request_wide or (
                exc.status is not None and exc.status not in TIMEOUT_STATUSES
            ):
                raise
            if _timed_out(exc):
                timeouts += 1
                timed_out += clock() - started
                if timeouts >= max_timeouts or timed_out >= max_timeout_seconds:
                    log.warning(
                        "stopping at %s: %d timeouts", describe(chunk), timeouts
                    )
                    out.remaining = chunk + [
                        i for rest in reversed(pending) for i in rest
                    ]
                    return out
            if len(chunk) == 1:
                log.warning("giving up on %s: %s", describe(chunk), exc)
                out.failed.append(chunk[0])
                continue
            middle = len(chunk) // 2
            log.info("splitting %s after: %s", describe(chunk), exc)
            pending.append(chunk[middle:])
            pending.append(chunk[:middle])
    return out
