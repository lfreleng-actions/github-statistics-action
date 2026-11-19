# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Scope resolution: targets to public repositories, and the store side (design §5)."""

from __future__ import annotations

from .apply import ScopeChanges, apply_scope
from .resolver import Dropped, Repository, ResolvedScope, resolve_scope

__all__ = [
    "Dropped",
    "Repository",
    "ResolvedScope",
    "ScopeChanges",
    "apply_scope",
    "resolve_scope",
]
