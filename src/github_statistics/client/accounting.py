# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""What one transport's requests cost: its stats and its budget charges."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx

from .budget import Budget, Resource
from .errors import BudgetExhaustedError, ClientError
from .policy import int_header


@dataclass
class RequestStats:
    """What a transport has done so far in a run."""

    requests: int = 0
    retries: Counter[str] = field(default_factory=Counter)
    not_modified: int = 0
    bytes_received: int = 0
    rate_limit_remaining: int | None = None
    rate_limit_reset: int | None = None
    graphql_cost: int = 0
    graphql_remaining: int | None = None


@dataclass
class Ledger:
    """One transport's stats and its ``Budget``, if any.

    ``authenticated`` says whether the transport sends a token, which
    alone makes a ``304`` free.
    """

    budget: Budget | None
    authenticated: bool
    stats: RequestStats = field(default_factory=RequestStats)

    def reserve(self, label: str, *, rest: bool) -> None:
        """Take one REST request for an attempt before it is sent.

        Raises ``BudgetExhaustedError`` when none is left.
        """
        if rest and self.budget is not None and not self.budget.try_spend_rest():
            raise self.refusal(label)

    def refusal(
        self,
        label: str,
        *,
        retry: bool = False,
        resource: Resource = "rest",
        spent: str | None = None,
    ) -> BudgetExhaustedError:
        """The error refusing a request, or its retry, for ``label``.

        It names the time limit when the deadline, not ``resource``'s
        count or rate limit, stopped it, so no refusal blames a budget
        the clock ended. Otherwise it says ``spent``, or by default that
        no REST budget is left.
        """
        if self.budget is not None and self.budget.stop_reason(resource) == "time":
            verb = "retrying" if retry else "sending"
            return BudgetExhaustedError(f"time limit reached; not {verb} {label}")
        if spent is not None:
            return BudgetExhaustedError(spent)
        if retry:
            return BudgetExhaustedError(f"no REST budget left to retry {label}")
        return BudgetExhaustedError(f"no REST budget left for {label}")

    def refund(self, *, rest: bool) -> None:
        """Give back an attempt's REST request, for one that never left."""
        if rest and self.budget is not None:
            self.budget.refund_rest()

    def record(self, response: httpx.Response, *, graphql: bool, rest: bool) -> None:
        """Account for ``response`` in the stats and the budget.

        Every REST response's core quota headers reach the budget, a
        ``304`` included. GitHub waives the charge for an authenticated
        ``304`` alone, so an anonymous one stays charged.
        """
        stats = self.stats
        stats.bytes_received += len(response.content)
        remaining = int_header(response, "x-ratelimit-remaining")
        reset = int_header(response, "x-ratelimit-reset")
        if remaining is not None:
            stats.rate_limit_remaining = remaining
        if reset is not None:
            stats.rate_limit_reset = reset
        if response.status_code == 304:
            stats.not_modified += 1
        budget = self.budget
        if budget is None or graphql:
            return
        core = response.headers.get("x-ratelimit-resource", "core") == "core"
        if core and remaining is not None:
            budget.observe(remaining, reset)
        if rest and response.status_code == 304 and self.authenticated:
            budget.refund_rest()

    def seed(self, response: httpx.Response, payload: Any) -> None:
        """Observe the core quota in a ``GET /rate_limit`` ``payload``.

        Raises ``ClientError`` when neither the payload nor the
        response's own core-resource headers carry a core count.
        """
        if self.budget is None:
            return
        resources = payload.get("resources") if isinstance(payload, dict) else None
        core = resources.get("core") if isinstance(resources, dict) else None
        remaining = core.get("remaining") if isinstance(core, dict) else None
        reset = core.get("reset") if isinstance(core, dict) else None
        header_core = response.headers.get("x-ratelimit-resource", "core") == "core"
        if isinstance(remaining, int):
            self.budget.observe(remaining, reset if isinstance(reset, int) else None)
        elif not header_core or int_header(response, "x-ratelimit-remaining") is None:
            raise ClientError("GET /rate_limit returned no core rate-limit count")
