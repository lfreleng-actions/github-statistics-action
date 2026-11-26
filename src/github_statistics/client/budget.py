# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The request budget of one target's run (design §6.5).

The budget is a courtesy limit, not exact accounting: GitHub enforces
the real rate limit, and the transport backs off on it. The budget lets
a run stop cleanly and leave headroom for other consumers. Nothing here
raises; the transport raises ``BudgetExhaustedError`` when a reservation
is refused, and a collector that meets it stops cleanly. An optional
deadline ends the run the same way: once it passes, the budget reports
itself spent.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

DEFAULT_REST = 3000
DEFAULT_GRAPHQL_POINTS = 1500
DEFAULT_RESERVE = 500
# GitHub allows an anonymous client 60 core requests an hour.
DEFAULT_ANONYMOUS_RESERVE = 10


@dataclass
class Budget:
    """REST requests and GraphQL points one transport may still spend.

    One ``Budget`` belongs to one ``Transport``: one token, one target.
    ``rest`` caps the REST requests and ``graphql_points`` the GraphQL
    points of that target's run. ``reserve`` is what the token keeps of
    ``x-ratelimit-remaining`` for other consumers; left at ``None``, the
    transport sets ``DEFAULT_RESERVE`` for a token and
    ``DEFAULT_ANONYMOUS_RESERVE`` for anonymous reads.

    Tolerance: the observed ``remaining`` only moves when a response
    reports it, so a run may overshoot ``rest``, or dip into the
    reserve, by at most the transport's concurrency (requests already in
    flight), and may overshoot ``graphql_points`` by at most the queries
    in flight. A response without a core ``x-ratelimit-remaining`` header
    leaves the observed count where it was; while GitHub sends none, only
    ``rest`` bounds the run. Exactness beyond that is a non-goal.

    A GraphQL rate limit stops the GraphQL budget for the rest of the
    run: ``graphql_rate_limited`` is never cleared, so a query that
    settles afterwards cannot reopen it.

    ``deadline``, a time on ``clock`` (``time.monotonic`` unless a test
    injects another), bounds how long the run may take: from it on,
    every reservation is refused as if both counts were spent, and
    ``timed_out`` tells the two apart. Requests already in flight finish;
    ``graphql_stopped`` then keeps a query that holds its points but has
    not been sent, or would retry, from sending. ``stopped_by_count``
    records that a count or a GraphQL rate limit stopped the run before
    the deadline did, so a report can tell which stopped it first. Only
    a stop counts: a refused reservation, a count found spent where a
    caller stops on it, or a rate limit; points a queued query holds
    and then gives back do not.
    """

    rest: int = DEFAULT_REST
    graphql_points: int = DEFAULT_GRAPHQL_POINTS
    reserve: int | None = None
    rest_spent: int = 0
    graphql_spent: int = 0
    remaining: int | None = None
    reset: int | None = None
    graphql_rate_limited: bool = False
    deadline: float | None = None
    clock: Callable[[], float] = field(
        default=time.monotonic, repr=False, compare=False
    )
    stopped_by_count: bool = field(default=False, init=False)

    def observe(self, remaining: int, reset: int | None = None) -> None:
        """Take the core ``remaining`` count of the window ending ``reset``.

        Within one window the lowest count wins, since responses arrive
        out of order; a later ``reset`` starts a new window and replaces
        the count, and an earlier one is stale and ignored.
        """
        if self.remaining is None or (
            reset is not None and self.reset is not None and reset > self.reset
        ):
            self.remaining, self.reset = remaining, reset
        elif reset is None or self.reset is None or reset == self.reset:
            self.remaining = min(self.remaining, remaining)
            self.reset = self.reset if reset is None else reset

    def try_spend_rest(self) -> bool:
        """Take one REST request before it is sent; false when none is left."""
        if self.rest_left == 0:
            return False
        self.rest_spent += 1
        return True

    def refund_rest(self) -> None:
        """Give back one REST request GitHub did not charge."""
        self.rest_spent = max(self.rest_spent - 1, 0)

    def try_spend_graphql(self, estimate: int) -> bool:
        """Take ``estimate`` GraphQL points before a query; false when not left."""
        estimate = max(estimate, 0)
        if self.graphql_exhausted:
            return False
        if self.graphql_left < estimate:
            self.stopped_by_count = True
            return False
        self.graphql_spent += estimate
        return True

    def settle_graphql(self, estimate: int, actual: int | None) -> None:
        """Replace a query's ``estimate`` with its ``actual`` cost, when known."""
        if actual is not None:
            self.graphql_spent = max(
                self.graphql_spent - max(estimate, 0) + max(actual, 0), 0
            )

    def exhaust_graphql(self) -> None:
        """Stop GraphQL for the run, after GitHub rate-limited a query."""
        if not self.timed_out:
            self.stopped_by_count = True
        self.graphql_rate_limited = True

    @property
    def graphql_stopped(self) -> bool:
        """True once GraphQL is done for the run: rate limited or timed out."""
        return self.graphql_rate_limited or self.timed_out

    @property
    def timed_out(self) -> bool:
        """True once the deadline, if any, has passed."""
        return self.deadline is not None and self.clock() >= self.deadline

    @property
    def rest_left(self) -> int:
        """REST requests left: the allowance, less the reserve once observed.

        Zero once the deadline has passed. Before it, finding none left
        sets ``stopped_by_count``: every caller stops on that.
        """
        if self.timed_out:
            return 0
        left = max(self.rest - self.rest_spent, 0)
        if self.remaining is not None:
            reserve = DEFAULT_RESERVE if self.reserve is None else self.reserve
            left = min(left, max(0, self.remaining - reserve))
        if left == 0:
            self.stopped_by_count = True
        return left

    @property
    def graphql_left(self) -> int:
        """GraphQL points left to spend; none once stopped or timed out."""
        if self.graphql_stopped:
            return 0
        return max(self.graphql_points - self.graphql_spent, 0)

    @property
    def rest_exhausted(self) -> bool:
        """True once no REST request is left."""
        return self.rest_left == 0

    @property
    def graphql_exhausted(self) -> bool:
        """True once no GraphQL point is left.

        Before the deadline, a true answer sets ``stopped_by_count``: a
        caller stops on it. ``graphql_left`` alone, read to size a batch,
        does not.
        """
        if self.graphql_left > 0:
            return False
        if not self.timed_out:
            self.stopped_by_count = True
        return True
