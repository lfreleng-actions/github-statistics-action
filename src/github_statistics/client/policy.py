# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The transport's request policy: URLs, headers, retries and redirects."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from typing import Any

import httpx

# Redirects a REST ``GET`` follows.
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Seconds to wait out a first secondary rate limit that names no retry
# time, as GitHub's documentation asks; each retry after it doubles.
SECONDARY_WAIT = 60.0

# Failures raised before the request left, so GitHub charged nothing.
UNSENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# GraphQL's lexical grammar: ignored tokens (a byte order mark, white
# space, line terminators, commas and comments), block and plain strings,
# names, and any other character as a punctuator of its own.
_GRAPHQL_TOKEN = re.compile(
    r"(?P<ignored>(?:[\ufeff\t\n\r ,]|#[^\r\n]*)+)"
    r'|(?P<string>"""(?:\\"""|(?!""")[\s\S])*"""|"(?:\\.|[^"\\\r\n])*")'
    r"|(?P<name>[_A-Za-z][_0-9A-Za-z]*)"
    r"|(?P<other>[\s\S])"
)
_WRITE_OPERATIONS = frozenset({"mutation", "subscription"})


def graphql_url(api_url: str) -> str:
    """The GraphQL endpoint for the REST base ``api_url``.

    ``https://api.github.com`` answers GraphQL at ``/graphql``; a GitHub
    Enterprise Server base ending ``/api/v3`` answers at ``/api/graphql``.
    """
    base = api_url.rstrip("/")
    if base.endswith("/api/v3"):
        return base[: -len("/v3")] + "/graphql"
    return base + "/graphql"


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header, or ``None`` when it does not parse.

    Only the delay-seconds form is read: GitHub sends no other, and the
    HTTP-date form would need the current time.
    """
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def int_header(response: httpx.Response, name: str) -> int | None:
    """Header ``name`` as an integer, or ``None`` when absent or malformed."""
    value = response.headers.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def origin(url: httpx.URL) -> tuple[str, str, int | None]:
    """Scheme, host and effective port of ``url``."""
    port = url.port
    if port is None:
        port = {"https": 443, "http": 80}.get(url.scheme)
    return url.scheme, url.host, port


def names_secondary_limit(message: Any) -> bool:
    """Whether ``message`` says GitHub applied a secondary rate limit."""
    return isinstance(message, str) and "secondary rate limit" in message.lower()


def errors_name_secondary(errors: list[Any]) -> bool:
    """Whether a GraphQL error list names a secondary rate limit."""
    return any(
        isinstance(error, dict) and names_secondary_limit(error.get("message"))
        for error in errors
    )


def _primary_error(error: Any) -> bool:
    """Whether one GraphQL error reports a primary rate limit.

    GitHub marks one ``type: RATE_LIMITED``, or ``type: RATE_LIMIT``
    with ``extensions.code: graphql_rate_limit``, often with ``data: null``.
    """
    if not isinstance(error, dict):
        return False
    extensions = error.get("extensions")
    code = extensions.get("code") if isinstance(extensions, dict) else None
    return error.get("type") in ("RATE_LIMITED", "RATE_LIMIT") or (
        code == "graphql_rate_limit"
    )


def errors_rate_limited(errors: list[Any]) -> bool:
    """Whether a GraphQL error list reports a rate limit, primary or secondary.

    A secondary limit may carry only a message naming it, even in an
    HTTP ``200``.
    """
    return any(map(_primary_error, errors)) or errors_name_secondary(errors)


def secondary_body(response: httpx.Response) -> bool:
    """Whether a ``403`` or ``429`` names a secondary rate limit in its body.

    GitHub may answer a secondary rate limit with neither ``Retry-After``
    nor ``x-ratelimit-remaining: 0``; its message, documentation link or
    an error message still names the limit. A primary ``RATE_LIMITED``
    error does not, so it stops only its own resource.
    """
    if response.status_code not in (403, 429):
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    link = body.get("documentation_url")
    errors = body.get("errors")
    return (
        names_secondary_limit(body.get("message"))
        or (isinstance(link, str) and "secondary-rate-limits" in link)
        or (isinstance(errors, list) and errors_name_secondary(errors))
    )


def rate_limited(response: httpx.Response) -> bool:
    """Whether ``response`` is a rate limit.

    A ``429`` is; so is a ``403`` carrying ``Retry-After`` or
    ``x-ratelimit-remaining: 0``, or whose body names a secondary rate
    limit. Any other ``403`` is a permission error.
    """
    status = response.status_code
    return status == 429 or (
        status == 403
        and (
            "retry-after" in response.headers
            or response.headers.get("x-ratelimit-remaining") == "0"
            or secondary_body(response)
        )
    )


def rate_limit_delay(response: httpx.Response, backoff: float) -> float:
    """Seconds to wait out rate-limited ``response``, given the exponential ``backoff``.

    ``Retry-After`` wins when it parses; then, with
    ``x-ratelimit-remaining: 0``, the time until its ``x-ratelimit-reset``
    epoch, or none once it has passed; a body naming a secondary limit
    with no retry time scales ``backoff``, which doubles from one second,
    from ``SECONDARY_WAIT``: 60, 120, then 240 seconds; anything else
    takes ``backoff``.
    """
    after = parse_retry_after(response.headers.get("retry-after"))
    if after is not None:
        return after
    reset = int_header(response, "x-ratelimit-reset")
    if response.headers.get("x-ratelimit-remaining") == "0" and reset is not None:
        return max(reset - time.time(), 0.0)
    if secondary_body(response):
        return SECONDARY_WAIT * backoff
    return backoff


def secondary_limit(response: httpx.Response) -> bool:
    """Whether the rate limit ``response`` reports may be a secondary one.

    A body naming a secondary limit is one, whatever the headers say.
    Otherwise only a primary limit reports ``x-ratelimit-remaining: 0``,
    and it bounds one resource; a secondary limit spans every API the
    token reaches, so any other rate limit is treated as one.
    """
    return (
        secondary_body(response) or response.headers.get("x-ratelimit-remaining") != "0"
    )


def write_operation(query: str) -> str | None:
    """The ``mutation`` or ``subscription`` keyword opening a definition in ``query``.

    Returns ``None`` for a document of queries and fragments. Comments,
    commas and string literals are skipped, and brackets are counted, so
    a keyword only counts at the start of a top-level definition. This
    guards the read-only client against misuse; it is not a security
    boundary, and a malformed document is left for GitHub to reject.
    """
    depth = 0
    at_definition = True
    for token in _GRAPHQL_TOKEN.finditer(query):
        kind, text = token.lastgroup, token.group()
        if kind in ("ignored", "string"):
            continue
        if at_definition and text in _WRITE_OPERATIONS:
            return text
        at_definition = False
        if text in ("{", "(", "["):
            depth += 1
        elif text in ("}", ")", "]"):
            depth = max(depth - 1, 0)
            at_definition = text == "}" and depth == 0
    return None


def retry_kind(response: httpx.Response) -> str | None:
    """Why ``response`` should be retried, or ``None`` to return it.

    The client only reads, so every rate limit and ``5xx`` is safe to resend.
    """
    if rate_limited(response):
        return "rate_limit"
    if response.status_code >= 500:
        return "server"
    return None


def with_params(url: httpx.URL, params: Mapping[str, Any] | None) -> httpx.URL:
    """``url`` with ``params`` merged into its query; ``params`` wins a shared key.

    ``httpx`` replaces a URL's query with ``params`` rather than merging
    them, which would silently drop a filter such as ``?state=all``.
    """
    return url.copy_merge_params(params) if params else url
