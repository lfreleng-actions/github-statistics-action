# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the ``collect`` command (design §4.2, §6.5)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
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


@pytest.mark.xfail(strict=True)
def test_a_passed_deadline_stops_after_the_scope_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Time running out in the scope step keeps the scope and says so."""
    import github_statistics.cli.collect as command
    import github_statistics.collect.run as orchestration
    from github_statistics.scope import Repository, ResolvedScope
    from github_statistics.store import Store, store_path

    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    now = [0.0]
    monkeypatch.setattr(command, "monotonic", lambda: now[0])

    async def scope(*_args: Any, **_kwargs: Any) -> Any:
        now[0] = 60.0
        repository = Repository(
            "a", "2020-01-01T00:00:00Z", False, False, False, "main"
        )
        return ResolvedScope("acme", "Organization", (repository,), ())

    monkeypatch.setattr(orchestration, "resolve_scope", scope)

    result = _invoke(tmp_path, "--max-minutes", "1")

    assert result.exit_code == 0, result.output
    text = summary.read_text(encoding="utf-8")
    assert "| scope (time limit reached) |" in text
    with Store(store_path(tmp_path, "api.github.com", "acme")) as store:
        assert store.in_scope(datetime.now(UTC).date()) == ["acme/a"]


@pytest.mark.xfail(strict=True, reason="time limit not implemented yet")
def test_a_passed_deadline_skips_seeding_the_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Once time is up, no rate limit read is sent and the summary still reports."""
    import github_statistics.cli.collect as command
    from github_statistics.cli import app

    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    clock = iter([0.0])
    monkeypatch.setattr(command, "monotonic", lambda: next(clock, 60.0))
    calls = _stub(monkeypatch, _result(exhausted="scope"))

    with respx.mock:
        seed = respx.get(f"{API}/rate_limit").respond(404, json={})
        result = runner.invoke(
            app,
            [
                "collect",
                "--repo",
                "acme/a",
                "--cache-dir",
                str(tmp_path),
                "--max-minutes",
                "1",
            ],
        )

    assert result.exit_code == 0, result.output
    assert seed.call_count == 0
    assert len(calls) == 1
    assert "| scope (time limit reached) |" in summary.read_text(encoding="utf-8")


@pytest.mark.xfail(strict=True)
def test_a_seed_failing_as_time_runs_out_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A seed retry would start past the deadline: none is sent, and collect reports."""
    import github_statistics.cli.collect as command
    from github_statistics.cli import app

    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    now = [0.0]
    monkeypatch.setattr(command, "monotonic", lambda: now[0])
    calls = _stub(monkeypatch, _result(exhausted="scope"))

    def fail_as_time_runs_out(_request: httpx.Request) -> httpx.Response:
        now[0] = 60.0
        return httpx.Response(502)

    with respx.mock:
        seed = respx.get(f"{API}/rate_limit").mock(side_effect=fail_as_time_runs_out)
        result = runner.invoke(
            app,
            ["collect", "--repo", "acme/a", "--cache-dir", str(tmp_path)]
            + ["--max-minutes", "1"],
        )

    assert result.exit_code == 0, result.output
    assert seed.call_count == 1
    assert len(calls) == 1
    assert "| scope (time limit reached) |" in summary.read_text(encoding="utf-8")


@pytest.mark.xfail(strict=True)
@pytest.mark.parametrize(
    ("delay", "woke", "stops"),
    [(5.0, 5.0, False), (10.0, 10.0, True), (5.0, 12.0, True)],
    ids=["before", "at-the-deadline", "late-wakeup"],
)
async def test_a_backoff_ending_at_or_past_the_deadline_stops(
    delay: float, woke: float, stops: bool
) -> None:
    """A retry is refused once its backoff ends at or past the deadline."""
    import github_statistics.cli.collect as command
    from github_statistics.client import Budget, BudgetExhaustedError

    now = [0.0]
    budget = Budget(rest=10, deadline=10.0, clock=lambda: now[0])

    async def sleep(_seconds: float) -> None:
        now[0] = woke

    backoff = command._backoff_within(budget, sleep=sleep)
    if stops:
        with pytest.raises(BudgetExhaustedError):
            await backoff(delay)
    else:
        await backoff(delay)


@pytest.mark.xfail(strict=True)
def test_a_count_spent_before_the_deadline_keeps_its_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A step a count stopped stays named when time runs out in a later one."""
    import github_statistics.cli.collect as command

    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    now = [0.0]
    monkeypatch.setattr(command, "monotonic", lambda: now[0])

    async def fake(_config: Any, _target: Any, *, transport: Any, **_: Any) -> Any:
        budget = transport.budget
        budget.rest_spent = budget.rest
        assert budget.rest_exhausted
        now[0] = 60.0
        assert budget.graphql_exhausted
        return _result(exhausted="backfill")

    monkeypatch.setattr(command, "collect", fake)

    result = _invoke(tmp_path, "--max-minutes", "1")

    assert result.exit_code == 0, result.output
    text = summary.read_text(encoding="utf-8")
    assert "| backfill; time limit reached later |" in text
