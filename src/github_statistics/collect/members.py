# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Read the public members of each member organisation (design §7.1).

The privacy rule lives here: the collector reads
``/orgs/{org}/public_members`` and nothing else, so the tool never
learns a private membership, whatever the token may see.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date
from urllib.parse import quote

import httpx

from github_statistics.client import (
    BudgetExhaustedError,
    Transport,
    TransportError,
    get_paged,
)
from github_statistics.store import Store

from .common import stop_cause

log = logging.getLogger(__name__)

USER = "User"


async def sync_members(
    transport: Transport,
    store: Store,
    member_orgs: Iterable[str],
    *,
    owner: str,
    owner_type: str,
    today: date,
) -> dict[str, int]:
    """Store the public members of each of ``member_orgs``; counts by organisation.

    Every page of ``GET /orgs/{org}/public_members`` is read, then the
    logins replace those stored (`Store.replace_members`). When
    ``owner_type`` is ``User``, ``owner`` names no organisation and is
    not read: the classifier counts the owner a member itself. A read
    that fails, by HTTP status, transport error or spent budget, keeps
    the stored list, logs a warning and leaves the organisation out of
    the result. A spent budget, or a rate limit that outlasts the
    retries, ends the step: ``BudgetExhaustedError`` propagates once the
    lists read before it are stored; the time limit is logged at
    ``INFO``, being the designed end of a run. ``AuthError`` propagates.
    """
    counts: dict[str, int] = {}
    for org in dict.fromkeys(member_orgs):
        if owner_type == USER and org.lower() == owner.lower():
            continue
        path = f"/orgs/{quote(org, safe='')}/public_members"
        logins: list[str] = []
        try:
            async for page, _response in get_paged(transport, path, {"per_page": 100}):
                logins.extend(member["login"] for member in page)
        except BudgetExhaustedError as exc:
            budget = transport.budget
            timed = budget is not None and budget.stop_reason("rest") == "time"
            level = logging.INFO if timed else logging.WARNING
            cause = stop_cause(transport, exc, "rest")
            log.log(level, "kept the stored members of %s: %s", org, cause)
            raise
        except (httpx.HTTPStatusError, TransportError) as exc:
            log.warning("kept the stored members of %s: %s", org, exc)
            continue
        store.replace_members(org, logins, today)
        counts[org] = len(logins)
    return counts
