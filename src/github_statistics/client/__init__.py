# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The GitHub client every collector uses (design §4.1, §6.5)."""

from __future__ import annotations

from .accounting import RequestStats
from .budget import Budget
from .errors import (
    AuthError,
    BudgetExhaustedError,
    ClientError,
    ForeignHostError,
    GraphQLError,
    TransportError,
)
from .graphql import Batched, batched, graphql
from .paging import Conditional, get_conditional, get_paged
from .policy import graphql_url, parse_retry_after
from .transport import Transport

__all__ = [
    "AuthError",
    "Batched",
    "Budget",
    "BudgetExhaustedError",
    "ClientError",
    "Conditional",
    "ForeignHostError",
    "GraphQLError",
    "RequestStats",
    "Transport",
    "TransportError",
    "batched",
    "get_conditional",
    "get_paged",
    "graphql",
    "graphql_url",
    "parse_retry_after",
]
