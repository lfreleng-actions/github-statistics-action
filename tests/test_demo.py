# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the ``demo`` command and its synthetic data."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

UNTIL = date(2026, 11, 16)


@pytest.mark.xfail(strict=True, reason="demo not implemented yet")
def test_demo_writes_six_pngs(tmp_path: Path) -> None:
    """Three charts, each as a master and a 1920 px derivative."""
    from github_statistics.cli import app

    result = runner.invoke(
        app,
        [
            "demo",
            "--output-dir",
            str(tmp_path),
            "--days",
            "30",
            "--until",
            "2026-11-16",
        ],
    )

    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "issues-1920.png",
        "issues.png",
        "pull-requests-1920.png",
        "pull-requests.png",
        "workflows-1920.png",
        "workflows.png",
    ]


@pytest.mark.xfail(strict=True, reason="demo not implemented yet")
def test_demo_rejects_a_window_shorter_than_a_week(tmp_path: Path) -> None:
    """The demo window is bounded to 7..730 days."""
    from github_statistics.cli import app

    result = runner.invoke(app, ["demo", "--output-dir", str(tmp_path), "--days", "6"])

    assert result.exit_code != 0
    assert "Invalid value for '--days'" in _ANSI.sub("", result.output)
    assert not list(tmp_path.iterdir())


@pytest.mark.xfail(strict=True, reason="demo not implemented yet")
def test_demo_data_has_the_promised_gaps_and_is_seeded() -> None:
    """Four no-data days and one partial day; one seed always gives the same data."""
    from github_statistics.render import Coverage
    from github_statistics.render.demo import demo_specs

    specs = demo_specs(365, 7, UNTIL)

    assert [name for name, _ in specs] == ["pull-requests", "issues", "workflows"]
    for _, spec in specs:
        assert len(spec.days) == 365
        assert spec.days[-1] == UNTIL
        assert spec.coverage.count(Coverage.NONE) == 4
        assert spec.coverage.count(Coverage.PARTIAL) == 1
    assert demo_specs(365, 7, UNTIL) == specs
    assert demo_specs(365, 8, UNTIL) != specs
