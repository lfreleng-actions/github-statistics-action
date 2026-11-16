# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Run the command line with ``python -m github_statistics``."""

from __future__ import annotations

from github_statistics.cli import app

if __name__ == "__main__":
    app(prog_name="github-statistics")
