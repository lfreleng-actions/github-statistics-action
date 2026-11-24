# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the ``collect`` command (design §4.2, §6.5)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

API = "https://api.github.com"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the cache, configuration and GitHub files into the test's directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for name in ("GHS_CONFIG", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(name, raising=False)


def _result(**changes: Any) -> Any:
    """A `CollectResult` for ``acme``, with ``changes`` applied."""
    from github_statistics.collect import CollectResult, MetricCoverage

    values: dict[str, Any] = {
        "owner": "acme",
        "requests": 7,
        "graphql_points": 2,
        "not_modified": 1,
        "coverage": {"issues": MetricCoverage(2, 1, 0)},
        "gap_days": 1,
        "unreadable": {},
        "dropped": {},
        "exhausted": None,
        "stalled": (),
    }
    values.update(changes)
    return CollectResult(**values)


def _stub(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> list[dict[str, Any]]:
    """Replace ``collect`` with one that returns or raises ``outcome``."""
    import github_statistics.cli.collect as command

    calls: list[dict[str, Any]] = []

    async def fake(_config: Any, target: Any, **kwargs: Any) -> Any:
        calls.append({"target": target, **kwargs})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(command, "collect", fake)
    return calls


def _invoke(tmp_path: Path, *args: str) -> Any:
    """Run ``collect`` for ``acme/a`` with the rate limit endpoint mocked."""
    from github_statistics.cli import app

    with respx.mock:
        respx.get(f"{API}/rate_limit").respond(404, json={})
        return runner.invoke(
            app,
            ["collect", "--repo", "acme/a", "--cache-dir", str(tmp_path), *args],
        )


def test_a_spent_budget_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run that spends its budget succeeds and says where it ran out."""
    calls = _stub(monkeypatch, _result(exhausted="backfill"))

    result = _invoke(tmp_path)

    assert result.exit_code == 0, result.output
    assert "backfill" in result.output
    assert calls[0]["refresh"] is False
    assert (tmp_path / "api.github.com").exists()


def test_refresh_reaches_collect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--refresh`` is passed through."""
    calls = _stub(monkeypatch, _result())

    result = _invoke(tmp_path, "--refresh")

    assert result.exit_code == 0, result.output
    assert calls[0]["refresh"] is True


def test_until_reaches_collect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--until`` is passed through as the window's end."""
    calls = _stub(monkeypatch, _result())

    result = _invoke(tmp_path, "--days", "7", "--until", "2026-10-31")

    assert result.exit_code == 0, result.output
    assert calls[0]["until"] == date(2026, 10, 31)


def test_strict_exits_two_on_an_unreadable_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--strict`` exits 2 after reporting; without it the run succeeds."""
    _stub(monkeypatch, _result(unreadable={"acme/a": "items: HTTP 502"}))

    lenient = _invoke(tmp_path)
    strict = _invoke(tmp_path, "--strict")

    assert lenient.exit_code == 0, lenient.output
    assert strict.exit_code == 2, strict.output
    assert "acme/a" in strict.output


def test_a_rejected_token_exits_three(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 401 names the token's variable, never its value, and exits 3."""
    from github_statistics.client import AuthError

    monkeypatch.setenv("GHS_TOKEN", "ghp_secretvalue")
    _stub(monkeypatch, AuthError("401 Unauthorized"))

    result = _invoke(tmp_path, "--token-env", "GHS_TOKEN")

    assert result.exit_code == 3
    assert "$GHS_TOKEN" in result.output
    assert "ghp_secretvalue" not in result.output


def test_writes_the_outputs_and_job_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The step outputs and a Markdown summary reach the GitHub files."""
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    _stub(monkeypatch, _result())

    result = _invoke(tmp_path)

    assert result.exit_code == 0, result.output
    assert output.read_text(encoding="utf-8").splitlines() == [
        "requests_used=7",
        "coverage_complete=false",
        "gap_days=1",
    ]
    text = summary.read_text(encoding="utf-8")
    assert "### Collection" in text
    assert "| acme | issues | 2 | 1 | 0 |" in text


def test_a_rate_limited_seed_reports_a_spent_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rate limit on seeding the budget still reports, writes and exits 0."""
    from github_statistics.cli import app

    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    with respx.mock:
        seed = respx.get(f"{API}/rate_limit").respond(
            429, headers={"retry-after": "3600"}
        )
        result = runner.invoke(
            app, ["collect", "--repo", "acme/a", "--cache-dir", str(tmp_path)]
        )

    assert result.exit_code == 0, result.output
    assert seed.call_count == 1
    assert "scope" in result.output
    assert output.read_text(encoding="utf-8").splitlines()[0] == "requests_used=1"
