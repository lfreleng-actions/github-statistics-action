# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The static site: a landing page and one page per target (design §8.5)."""

from __future__ import annotations

from .build import SiteTarget, build_site

__all__ = ["SiteTarget", "build_site"]
