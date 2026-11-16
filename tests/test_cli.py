# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the github-statistics command line skeleton."""

from __future__ import annotations

import re
import subprocess
import sys

import pytest
from typer.testing import CliRunner

import github_statistics

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI styling, which Rich adds when the environment forces colour."""
    return _ANSI.sub("", text)


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag_prints_version(flag: str) -> None:
    """The version flag prints the package version and exits 0."""
    from github_statistics.cli import app

    result = runner.invoke(app, [flag])

    assert result.exit_code == 0
    assert github_statistics.__version__ in _plain(result.output)


def test_no_arguments_prints_help() -> None:
    """Running with no arguments prints the help text."""
    from github_statistics.cli import app

    result = runner.invoke(app, [])

    output = _plain(result.output)
    assert "Usage" in output
    assert "--version" in output


def test_module_entry_point_prints_version() -> None:
    """``python -m github_statistics --version`` prints the version."""
    result = subprocess.run(
        [sys.executable, "-m", "github_statistics", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert github_statistics.__version__ in result.stdout
