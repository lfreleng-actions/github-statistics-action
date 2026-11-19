# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Errors the GitHub client raises (design §4.1 ``client/``)."""

from __future__ import annotations

from typing import Any


class ClientError(Exception):
    """Base class for every error the GitHub client raises."""


class AuthError(ClientError):
    """GitHub rejected the token (HTTP 401); the run must stop."""


class TransportError(ClientError):
    """A request could not complete within the retry and wait budget."""


class BudgetExhaustedError(ClientError):
    """The budget has no REST request or GraphQL point left for a request.

    Also raised, unsent, for a resource a rate limit that outlasted a
    request's retries has stopped for the run.

    Not a failure of the run: a collector that meets it stops cleanly,
    as it would on finding ``rest_exhausted`` or ``graphql_exhausted``
    set (design §6.5).
    """


class ForeignHostError(ClientError):
    """A request named a host other than the configured API host."""


class GraphQLError(ClientError):
    """A GraphQL query failed, by error payload or by HTTP status.

    ``status`` is the HTTP status when GitHub did not answer ``200``,
    and ``None`` when it answered ``200`` with an ``errors`` payload.
    ``request_wide`` is true when the failure is not the fault of any id
    in the query (a rate limit, or a query GitHub refused to run), so a
    smaller batch cannot avoid it.
    """

    def __init__(
        self,
        message: str,
        *,
        errors: list[dict[str, Any]] | None = None,
        status: int | None = None,
        request_wide: bool = False,
    ) -> None:
        """Record the message, the ``errors`` payload, the status and its scope."""
        super().__init__(message)
        self.errors = errors or []
        self.status = status
        self.request_wide = request_wide
