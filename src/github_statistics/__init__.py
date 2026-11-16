# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Daily, visual activity statistics for GitHub organisations and repositories."""

from __future__ import annotations

try:
    from ._version import __version__ as _vcs_version
except ImportError:  # pragma: no cover - no _version.py without a git build
    _vcs_version = "0.0.0"

__version__: str = _vcs_version

__all__ = ["__version__"]
