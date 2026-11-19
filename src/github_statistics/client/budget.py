# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The request budget of one target's run (design §6.5).

The budget is a courtesy limit, not exact accounting: GitHub enforces
the real rate limit, and the transport backs off on it. The budget lets
a run stop cleanly and leave headroom for other consumers. Nothing here
raises; the transport raises ``BudgetExhaustedError`` when a reservation
is refused, and a collector that meets it stops cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    run: ``graphql_stopped`` is never cleared, so a query that settles
    afterwards cannot reopen it.
    """

    rest: int = DEFAULT_REST
    graphql_points: int = DEFAULT_GRAPHQL_POINTS
    reserve: int | None = None
    rest_spent: int = 0
    graphql_spent: int = 0
    remaining: int | None = None
    reset: int | None = None
    graphql_stopped: bool = False

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
        if self.graphql_exhausted or self.graphql_left < estimate:
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
        self.graphql_stopped = True

    @property
    def rest_left(self) -> int:
        """REST requests left: the allowance, less the reserve once observed."""
        left = max(self.rest - self.rest_spent, 0)
        if self.remaining is None:
            return left
        reserve = DEFAULT_RESERVE if self.reserve is None else self.reserve
        return min(left, max(0, self.remaining - reserve))

    @property
    def graphql_left(self) -> int:
        """GraphQL points left to spend; none once GraphQL is stopped."""
        if self.graphql_stopped:
            return 0
        return max(self.graphql_points - self.graphql_spent, 0)

    @property
    def rest_exhausted(self) -> bool:
        """True once no REST request is left."""
        return self.rest_left == 0

    @property
    def graphql_exhausted(self) -> bool:
        """True once no GraphQL point is left."""
        return self.graphql_left == 0
