# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The one HTTP transport every GitHub request goes through (design §4.1).

``Transport`` owns authentication, the concurrency limit, the retry and
backoff policy, redirects, request accounting and the budget charge. The
token goes only to the configured API host, over HTTPS unless the caller
opts out, and is never logged.

The client is read-only by construction (design §9.4): it sends a REST
``GET`` through ``request()`` and a GraphQL query, a ``POST`` to the
GraphQL endpoint, through ``graphql()``, and nothing else. ``request()``
refuses any other method and ``graphql()`` refuses a mutation or a
subscription, both before sending. Every request is therefore safe to
resend, and one retry rule covers them all.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx

from .accounting import Ledger, RequestStats
from .budget import DEFAULT_ANONYMOUS_RESERVE, DEFAULT_RESERVE, Budget
from .cooldown import ALL, Cooldown
from .errors import AuthError, BudgetExhaustedError, ForeignHostError, TransportError
from .policy import (
    REDIRECT_STATUSES,
    UNSENT,
    graphql_url,
    origin,
    rate_limit_delay,
    retry_kind,
    secondary_limit,
    with_params,
)

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "github-statistics"
MAX_WAIT = 120.0
FIRST_BACKOFF = 1.0
MAX_REDIRECTS = 5

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class _Call:
    """One request: a REST ``GET``, or a GraphQL query ``POST`` with its body."""

    target: httpx.URL
    headers: dict[str, str]
    json: Any = None
    graphql: bool = False

    @property
    def method(self) -> str:
        """``POST`` for a GraphQL query, ``GET`` for anything else."""
        return "POST" if self.graphql else "GET"


@dataclass
class _Progress:
    """Retries and waiting spent on one request, across all its redirect hops."""

    retries: int = 0
    waited: float = 0.0
    sent: bool = False


class Transport:
    """An async context manager over one ``httpx.AsyncClient``.

    Every request, a REST ``GET`` or a GraphQL query, applies this policy:

    - a transport failure retries with exponential backoff, then raises
      ``TransportError``;
    - a ``401`` raises ``AuthError`` at once, without a retry, and stops
      the transport: every queued or later attempt raises it unsent;
    - a rate limit (``policy.rate_limited``) backs off as
      ``policy.rate_limit_delay`` says, pausing every other attempt for
      its resource, or every resource for a possibly secondary limit,
      until the backoff ends, even if the request is cancelled; one that
      outlasts the retries stops that resource, or every resource, for
      the run, and every later attempt on it raises
      ``BudgetExhaustedError`` unsent;
    - any ``5xx`` retries on the same schedule;
    - a ``GET``'s ``301``, ``302``, ``303``, ``307`` or ``308`` is
      followed, up to ``MAX_REDIRECTS`` hops, when its ``Location``
      shares the API origin, and refused otherwise; any other redirect,
      and any redirect of a GraphQL query, returns unfollowed;
    - anything else returns to the caller, including a ``403`` that is a
      real permission error.

    Backoff starts at one second and doubles, for at most ``max_retries``
    retries and ``max_wait`` seconds of total waiting per request, its
    redirect hops together. When retries run out on a response, that
    last response returns to the caller, unless it is a rate limit and
    the transport has a budget. Every response discarded for a retry or
    a redirect is closed.

    With a ``Budget``, which belongs to this transport alone, every
    chargeable attempt, retries and redirect hops included, reserves one
    REST request before it is sent and raises ``BudgetExhaustedError``
    when none is left; a retry that could not be sent raises it before
    backing off. So does every final rate-limit response, closed first,
    so the collector stops cleanly and keeps its partial coverage. A
    rate-limited GraphQL query retries like any other request; when
    retries run out it raises it and stops the GraphQL budget, and a
    GraphQL attempt checks that stop once it holds a connection slot,
    and raises it unsent.
    """

    def __init__(
        self,
        token: str | None,
        *,
        api_url: str = DEFAULT_API_URL,
        concurrency: int = 8,
        max_retries: int = 5,
        timeout: float = 30.0,
        budget: Budget | None = None,
        sleep: Sleep = asyncio.sleep,
        max_wait: float = MAX_WAIT,
        insecure: bool = False,
    ) -> None:
        """Configure the transport; ``sleep`` is injectable so tests run instantly.

        A token travels over HTTPS alone: an ``api_url`` with any other
        scheme raises ``ValueError`` unless ``insecure`` opts in.
        """
        if concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, not {concurrency}")
        if token is not None and not insecure and httpx.URL(api_url).scheme != "https":
            raise ValueError(f"refusing to send a token to non-HTTPS {api_url!r}")
        self.api_url = api_url.rstrip("/")
        self.graphql_url = graphql_url(self.api_url)
        self.budget = budget
        self.max_retries = max_retries
        self.max_wait = max_wait
        self._token = token
        self._rejected = False
        self._origin = origin(httpx.URL(self.api_url))
        self._cooldown = Cooldown(httpx.URL(self.api_url).path)
        if budget is not None and budget.reserve is None:
            budget.reserve = (
                DEFAULT_RESERVE if token is not None else DEFAULT_ANONYMOUS_RESERVE
            )
        self._ledger = Ledger(budget, authenticated=token is not None)
        self.stats: RequestStats = self._ledger.stats
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
        )

    def __repr__(self) -> str:
        """Name the API host only; never the token."""
        return f"Transport(api_url={self.api_url!r})"

    async def __aenter__(self) -> Transport:
        """Enter the context; the client is already open."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the client and its connection pool."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the client and its connection pool, ending any pause."""
        self._cooldown.close()
        await self._client.aclose()

    def stop(self, secondary: bool, resource: str = "graphql") -> None:
        """Stop ``resource``, or every resource if ``secondary``, for the run."""
        self._cooldown.stop(ALL if secondary else resource)

    @property
    def rest_left(self) -> int | None:
        """REST requests this transport may still spend, if budgeted."""
        if self.budget is None:
            return None
        return self.budget.rest_left

    @property
    def rest_exhausted(self) -> bool:
        """True once the budget has no REST request left."""
        return self.rest_left == 0

    def resolve(self, url: str) -> httpx.URL:
        """``url`` made absolute against ``api_url``; refuses any other host.

        A path joins ``api_url``. An absolute URL, such as one from a
        ``Link`` header, must share the scheme, host and port of
        ``api_url``, so neither a header nor a crafted URL can send the
        token elsewhere.
        """
        if url.startswith(("http://", "https://")):
            target = httpx.URL(url)
        else:
            target = httpx.URL(self.api_url + "/" + url.lstrip("/"))
        if origin(target) != self._origin:
            raise ForeignHostError(
                f"refusing a request to {target.host!r}: the API host is "
                f"{self._origin[1]!r}"
            )
        return target

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        charge: bool = True,
    ) -> httpx.Response:
        """Issue one REST ``GET`` under the retry policy and return its response.

        ``method`` must be ``GET``: the client is read-only, so any other
        method raises ``ValueError`` before anything is sent. ``params``
        merge into any query ``url`` carries. ``charge=False`` keeps the
        request off the REST budget (for the free ``GET /rate_limit``), and
        an authenticated ``304`` is refunded.
        """
        if method.upper() != "GET":
            raise ValueError(f"the client is read-only; refusing {method} {url}")
        target = with_params(self.resolve(url), params)
        call = _Call(target, self._headers(headers))
        return await self._follow(call, charge, _Progress())

    async def _post_graphql(self, body: Any, estimate: int) -> httpx.Response:
        """Post one query for ``graphql()``, holding ``estimate`` points first.

        Raises ``AuthError`` after a ``401``, before taking any points, and
        ``BudgetExhaustedError`` when they are not left. The query stays off
        the REST budget, and its points come back if no attempt left. A
        redirect returns unfollowed: GitHub does not redirect its GraphQL
        endpoint.
        """
        self._refuse_rejected("POST /graphql")
        budget = self.budget
        if budget is not None and not budget.try_spend_graphql(estimate):
            spent = f"no GraphQL budget left for a query costing {estimate}"
            raise self._ledger.refusal("POST /graphql", resource="graphql", spent=spent)
        target = self.resolve(self.graphql_url)
        call = _Call(target, self._headers(None), body, graphql=True)
        progress = _Progress()
        try:
            return await self._retrying(call, True, progress)
        except BaseException:
            if budget is not None and not progress.sent:
                budget.settle_graphql(estimate, 0)
            raise

    def _headers(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        """``headers`` with the token, for a URL ``resolve`` has accepted."""
        sent = dict(headers or {})
        if self._token is not None:
            sent["Authorization"] = f"Bearer {self._token}"
        return sent

    async def _follow(
        self, call: _Call, charge: bool, progress: _Progress
    ) -> httpx.Response:
        """Send a ``GET``, following same-origin redirects up to ``MAX_REDIRECTS``."""
        hops = 0
        while True:
            response = await self._retrying(call, charge, progress)
            location = response.headers.get("location")
            if (
                response.status_code not in REDIRECT_STATUSES
                or location is None
                or hops >= MAX_REDIRECTS
            ):
                return response
            await response.aclose()
            call = _Call(self.resolve(str(call.target.join(location))), call.headers)
            hops += 1
            log.debug("following %s to %s", response.status_code, call.target.path)

    async def _retrying(
        self, call: _Call, charge: bool, progress: _Progress
    ) -> httpx.Response:
        """Send ``call`` under the retry policy, spending from ``progress``."""
        method, target = call.method, call.target
        budget = self.budget
        chargeable = charge and budget is not None and not call.graphql
        resource = self._cooldown.resource(target, call.graphql)
        while True:
            retries, waited = progress.retries, progress.waited
            delay = FIRST_BACKOFF * 2**retries
            scope = resource
            discard: httpx.Response | None = None
            try:
                response = await self._attempt(call, chargeable, progress)
            except httpx.TransportError as exc:
                if retries >= self.max_retries or waited + delay > self.max_wait:
                    raise TransportError(
                        f"{method} {target.copy_with(query=None)} failed after "
                        f"{retries} retries: {type(exc).__name__}"
                    ) from exc
                kind = "transport"
            else:
                if response.status_code == 401:
                    await response.aclose()
                    raise AuthError(
                        f"GitHub rejected the token (401) for {method} {target.path}"
                    )
                why = retry_kind(response)
                if why is None:
                    return response
                kind = why
                if kind == "rate_limit":
                    delay = rate_limit_delay(response, delay)
                    scope = ALL if secondary_limit(response) else resource
                final = retries >= self.max_retries or waited + delay > self.max_wait
                if kind == "rate_limit" and final:
                    self._cooldown.stop(scope)
                    if budget is not None:
                        await response.aclose()
                        if call.graphql:
                            budget.exhaust_graphql()
                        raise BudgetExhaustedError(
                            f"rate limited past the retries for {method} {target.path}"
                        )
                if final:
                    return response
                discard = response
            # Paused before the first await, so no queued attempt slips past.
            pause = None
            if kind == "rate_limit":
                pause = self._cooldown.pause(scope, self._sleep(delay))
            if discard is not None:
                await discard.aclose()
            if chargeable and self.rest_exhausted:
                raise self._ledger.refusal(f"{method} {target.path}", retry=True)
            progress.retries += 1
            progress.waited += delay
            self.stats.retries[kind] += 1
            log.debug("retrying %s %s in %.1fs (%s)", method, target.path, delay, kind)
            await (self._sleep(delay) if pause is None else asyncio.shield(pause))

    def _refuse_rejected(self, label: str) -> None:
        """Raise ``AuthError`` once GitHub has rejected this transport's token."""
        if self._rejected:
            raise AuthError(f"GitHub rejected the token earlier; not sending {label}")

    async def _attempt(
        self, call: _Call, chargeable: bool, progress: _Progress
    ) -> httpx.Response:
        """Send one attempt, reserving its REST request first when ``chargeable``.

        It waits out a rate-limit pause on its resource before taking a
        connection slot, and gives the slot back to wait again if a pause
        began meanwhile, so a paused resource never blocks another's
        traffic. Once it holds a slot, an attempt raises unsent after a
        ``401`` on this transport, and raises ``BudgetExhaustedError``
        unsent once a terminal rate limit has stopped its resource, or
        for a query once GraphQL is stopped.
        """
        label = f"{call.method} {call.target.path}"
        resource = self._cooldown.resource(call.target, call.graphql)
        while True:
            await self._cooldown.wait(resource)
            await self._semaphore.acquire()
            if not self._cooldown.held(resource):
                break
            self._semaphore.release()
        try:
            self._refuse_rejected(label)
            budget = self.budget
            spent = f"the {resource} rate limit is spent; not sending {label}"
            if self._cooldown.stopped(resource):
                raise BudgetExhaustedError(spent)
            if call.graphql and budget is not None and budget.graphql_stopped:
                raise self._ledger.refusal(label, resource="graphql", spent=spent)
            self._ledger.reserve(label, rest=chargeable)
            self.stats.requests += 1
            try:
                response = await self._client.request(
                    call.method, call.target, json=call.json, headers=call.headers
                )
            except UNSENT:
                self._ledger.refund(rest=chargeable)
                raise
            except BaseException:
                progress.sent = True
                raise
            progress.sent = True
            if response.status_code == 401:
                self._rejected = True
        finally:
            self._semaphore.release()
        self._ledger.record(response, graphql=call.graphql, rest=chargeable)
        return response

    async def seed_budget(self) -> None:
        """Seed the budget from ``GET /rate_limit``, which costs nothing.

        A server without rate limiting (some GitHub Enterprise Server
        installs answer ``404``) leaves the budget unseeded; any other
        failure raises ``httpx.HTTPStatusError``, and a success with no
        core count raises ``ClientError``, since collecting with an
        unknown reserve could drain the token.
        """
        if self.budget is None:
            return
        response = await self.request("GET", "/rate_limit", charge=False)
        if response.status_code == 404:
            return
        response.raise_for_status()
        self._ledger.seed(response, response.json())
