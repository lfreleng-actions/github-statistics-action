# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""REST pagination and conditional requests over a ``Transport``."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .transport import Transport


def _items(body: Any, items_key: str | None) -> list[Any]:
    """The list of items in one page ``body``."""
    if items_key is not None:
        body = body[items_key]
    if not isinstance(body, list):
        raise ValueError("page body is not a list; name its items_key")
    return body


async def get_paged(
    transport: Transport,
    url: str,
    params: Mapping[str, Any] | None = None,
    *,
    items_key: str | None = None,
) -> AsyncIterator[tuple[list[Any], httpx.Response]]:
    """Yield ``(items, response)`` for each page, following ``rel="next"``.

    ``items_key`` names the list in an object body (``workflow_runs``);
    a list body needs none. The caller decides when to stop. A page that
    is not a success raises ``httpx.HTTPStatusError``. The next link
    already carries the query, so ``params`` goes with the first page
    only. A relative link resolves against the page it came from, and
    the transport refuses a link to any other host.
    """
    next_url: str | None = url
    next_params = params
    while next_url is not None:
        response = await transport.request("GET", next_url, params=next_params)
        response.raise_for_status()
        yield _items(response.json(), items_key), response
        link = response.links.get("next")
        target = link.get("url") if link else None
        next_url = str(response.url.join(target)) if target else None
        next_params = None


@dataclass(frozen=True)
class Conditional:
    """The answer to a conditional request.

    ``not_modified`` is true for a ``304``, which carries no body and
    keeps the ETag sent; otherwise ``body`` is the decoded JSON and
    ``etag`` the new ETag, ``None`` when GitHub sent none.
    """

    not_modified: bool
    body: Any
    etag: str | None
    response: httpx.Response


async def get_conditional(
    transport: Transport,
    url: str,
    params: Mapping[str, Any] | None = None,
    etag: str | None = None,
) -> Conditional:
    """``GET url`` with ``If-None-Match: etag``.

    GitHub charges no rate limit for a ``304`` to an authenticated
    request; an anonymous one is charged like any other.

    A response that is neither ``304`` nor a success raises
    ``httpx.HTTPStatusError``.
    """
    headers = {"If-None-Match": etag} if etag else None
    response = await transport.request("GET", url, params=params, headers=headers)
    if response.status_code == 304:
        return Conditional(not_modified=True, body=None, etag=etag, response=response)
    response.raise_for_status()
    return Conditional(
        not_modified=False,
        body=response.json(),
        etag=response.headers.get("etag"),
        response=response,
    )
