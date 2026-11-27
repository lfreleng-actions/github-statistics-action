# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the command line's logging and the progress lines of `collect`."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from .test_collect_run import (
    API,
    NOW,
    _config,
    _Fakes,
    _instant,
    _run,
    _scope,
    _store,
    _watermark,
)

runner = CliRunner()

TOKEN = "ghp_progress_lines_never_show_this"
QUIET = ("httpx", "httpcore", "matplotlib")
STEP = re.compile(r"^(starting|finished) the (\w+) step; ")
REPOSITORY = re.compile(r"^(\w+) step: (\S+): (items|runs) read over \d+ pages, ")


@pytest.fixture(autouse=True)
def _levels(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Restore the logger levels a command sets; keep the environment out."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("XDG_CONFIG_HOME", "GHS_CONFIG", "GHS_LOG_LEVEL", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    loggers = [logging.getLogger(), *(logging.getLogger(name) for name in QUIET)]
    saved = [logger.level for logger in loggers]
    yield
    for logger, level in zip(loggers, saved, strict=True):
        logger.setLevel(level)


def _invoke(*args: str, **env: str) -> Any:
    """Run ``config path`` with the root options ``args`` and ``env``."""
    from github_statistics.cli import app

    return runner.invoke(app, [*args, "config", "path"], env=env)


@pytest.mark.parametrize(
    ("args", "env", "level"),
    [
        ((), {}, logging.INFO),
        (("--log-level", "debug"), {}, logging.DEBUG),
        ((), {"GHS_LOG_LEVEL": "error"}, logging.ERROR),
        (("--log-level", "WARNING"), {"GHS_LOG_LEVEL": "debug"}, logging.WARNING),
    ],
)
def test_the_root_option_sets_the_level(
    args: tuple[str, ...], env: dict[str, str], level: int
) -> None:
    """``--log-level`` sets the level, falls back to the variable and wins over it."""
    result = _invoke(*args, **env)

    assert result.exit_code == 0, result.output
    assert logging.getLogger().level == level


def test_third_party_loggers_stay_at_warning_by_default() -> None:
    """``httpx`` logs each request URL at INFO, so it stays quieter."""
    assert _invoke().exit_code == 0
    assert all(logging.getLogger(name).level == logging.WARNING for name in QUIET)

    assert _invoke("--log-level", "debug").exit_code == 0
    assert all(logging.getLogger(name).level == logging.DEBUG for name in QUIET)


async def _collect_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, token: Any
) -> list[logging.LogRecord]:
    """Run `collect` over the fakes for ``acme/a`` and ``acme/b``; its records."""
    from github_statistics.client import Budget, Transport

    store = _store()
    _watermark(store, "acme/a")
    fakes = _Fakes(monkeypatch, _scope("a", "b"))
    config = _config()

    async def resolve_scope(transport: Any, target: Any, **kw: Any) -> Any:
        await transport.request("GET", "/orgs/acme")
        return await fakes.resolve_scope(transport, target, **kw)

    monkeypatch.setattr(_run(), "resolve_scope", resolve_scope)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{API}/orgs/acme").mock(return_value=httpx.Response(200, json={}))
        async with Transport(token, sleep=_instant, budget=Budget()) as transport:
            with caplog.at_level(logging.DEBUG):
                await _run().collect(
                    config,
                    config.targets[0],
                    now=NOW,
                    transport=transport,
                    store=store,
                )
    return list(caplog.records)


async def test_collect_logs_each_step_and_repository(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A start and end line per step; one line per repository and step."""
    records = await _collect_logged(monkeypatch, caplog, None)
    messages = [r.getMessage() for r in records if r.levelno == logging.INFO]

    steps = Counter(m.group(1, 2) for m in map(STEP.match, messages) if m)
    names = {"scope", "members", "incremental", "enrichment", "backfill", "sweep"}
    assert {name for _, name in steps} == names
    assert all(steps["starting", n] == steps["finished", n] for n in names)
    assert "REST requests" in next(m for m in messages if STEP.match(m))

    lines = Counter(m.group(1, 2) for m in map(REPOSITORY.match, messages) if m)
    assert set(lines.values()) == {1}
    assert set(lines) == {
        (step, key)
        for step in ("incremental", "backfill", "sweep")
        for key in ("acme/a", "acme/b")
    }
    joined = {
        m.group(1, 2)
        for m in map(REPOSITORY.match, messages)
        if m and "; runs read" in m.string
    }
    assert joined == {("incremental", "acme/a"), ("backfill", "acme/b")}


async def test_no_record_carries_the_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Even at DEBUG, with a request sent, the token appears in no record."""
    records = await _collect_logged(monkeypatch, caplog, TOKEN)

    assert any(STEP.match(r.getMessage()) for r in records)
    assert all(TOKEN not in r.getMessage() for r in records)
    assert all(TOKEN not in str(r.args) for r in records)


@pytest.mark.parametrize("budgeted", [True, False])
async def test_step_lines_count_rest_from_the_budget(
    caplog: pytest.LogCaptureFixture, budgeted: bool
) -> None:
    """GraphQL and uncharged requests count as HTTP requests, never as REST."""
    from github_statistics.client import Budget, Transport
    from github_statistics.collect.steps import Steps

    async def nothing() -> None:
        return None

    budget = Budget(rest_spent=2) if budgeted else None
    async with Transport(None, sleep=_instant, budget=budget) as transport:
        transport.stats.requests = 5
        with caplog.at_level(logging.INFO):
            await Steps(transport).run("scope", nothing())

    line = next(r.getMessage() for r in caplog.records if STEP.match(r.getMessage()))
    expected = "2 REST requests" if budgeted else "5 HTTP requests"
    assert line.split("; ")[1].startswith(expected)


async def test_a_sweep_line_comes_when_its_repository_finishes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A repository's sweep line does not wait for a slower sibling."""
    from github_statistics.client import Transport
    from github_statistics.collect.steps import Steps, each
    from github_statistics.collect.sweep import SweepResult

    gate = asyncio.Event()

    async def sweep(key: str, wait: bool) -> SweepResult:
        if wait:
            await gate.wait()
        return SweepResult(key, 1, 1, True, True)

    async with Transport(None, sleep=_instant) as transport:
        steps = Steps(transport)
        with caplog.at_level(logging.INFO):
            task = asyncio.create_task(
                each(
                    steps,
                    "sweep",
                    (
                        steps.reading(key, "runs", sweep(key, key == "acme/b"))
                        for key in ("acme/a", "acme/b")
                    ),
                )
            )
            for _ in range(10):
                await asyncio.sleep(0)
            assert _logged(caplog) == {("sweep", "acme/a")}
            gate.set()
            await task
    assert _logged(caplog) == {("sweep", "acme/a"), ("sweep", "acme/b")}


async def test_item_lines_alone_come_when_a_step_reading_nothing_ends(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Another listing keeps item lines held; with no runs read, enrichment ends it."""
    from github_statistics.client import Transport
    from github_statistics.collect import ItemSyncResult
    from github_statistics.collect.steps import Steps, each

    async def sync(key: str) -> dict[str, ItemSyncResult]:
        return {key: ItemSyncResult(key, 3, 1, True)}

    async def nothing() -> None:
        return None

    async with Transport(None, sleep=_instant) as transport:
        steps = Steps(transport)
        with caplog.at_level(logging.INFO):
            for step, key in (("incremental", "acme/a"), ("backfill", "acme/b")):
                await each(steps, step, [steps.reading(key, "items", sync(key))])
            assert _logged(caplog) == set()
            await steps.run("enrichment", nothing())
    assert _logged(caplog) == {("incremental", "acme/a"), ("backfill", "acme/b")}


async def test_an_item_line_joins_its_runs_across_other_steps(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Run counts join their step's item line though other steps came between."""
    from github_statistics.client import Transport
    from github_statistics.collect import ItemSyncResult
    from github_statistics.collect.steps import Steps
    from github_statistics.collect.sweep import SweepResult

    async def sync(key: str) -> dict[str, ItemSyncResult]:
        return {key: ItemSyncResult(key, 3, 1, True)}

    async def runs(key: str) -> SweepResult:
        return SweepResult(key, 2, 4, True, True)

    async def read(steps: Steps, step: str, key: str, what: str) -> None:
        work = sync(key) if what == "items" else runs(key)
        await steps.run(step, steps.reading(key, what, work))

    async with Transport(None, sleep=_instant) as transport:
        steps = Steps(transport)
        with caplog.at_level(logging.INFO):
            await read(steps, "incremental", "acme/a", "items")
            await read(steps, "backfill", "acme/b", "items")
            await read(steps, "incremental", "acme/a", "runs")
            await read(steps, "incremental", "acme/b", "runs")
            await read(steps, "backfill", "acme/b", "runs")
    lines = [r.getMessage() for r in caplog.records if REPOSITORY.match(r.getMessage())]
    assert lines == [
        "incremental step: acme/a: items read over 1 pages, 3 stored; "
        "runs read over 4 pages, 2 stored",
        "incremental step: acme/b: runs read over 4 pages, 2 stored",
        "backfill step: acme/b: items read over 1 pages, 3 stored; "
        "runs read over 4 pages, 2 stored",
    ]


async def test_a_phase_cut_short_lets_its_later_item_line_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A repository whose recent runs did not finish reads no backfill."""
    from types import SimpleNamespace

    from github_statistics.client import Transport
    from github_statistics.collect import ItemSyncResult
    from github_statistics.collect.steps import Steps, each

    async def sync(key: str) -> dict[str, ItemSyncResult]:
        return {key: ItemSyncResult(key, 3, 1, True)}

    def run_sync(key: str) -> Any:
        result = SimpleNamespace(runs=2, pages=4, unreadable=None)
        return SimpleNamespace(key=key, result=lambda: result)

    async with Transport(None, sleep=_instant) as transport:
        steps = Steps(transport)
        with caplog.at_level(logging.INFO):
            for key in ("acme/a", "acme/b"):
                await each(steps, "backfill", [steps.reading(key, "items", sync(key))])
            syncs = [run_sync("acme/a"), run_sync("acme/b")]
            steps.phases("incremental", syncs, [True, None])
            assert _logged(caplog) == {
                ("incremental", "acme/a"),
                ("backfill", "acme/b"),
            }
            steps.phases("backfill", syncs[:1], [True])
    lines = [r.getMessage() for r in caplog.records if REPOSITORY.match(r.getMessage())]
    assert lines[-1] == (
        "backfill step: acme/a: items read over 1 pages, 3 stored; "
        "runs read over 0 pages, 0 stored"
    )


@pytest.mark.parametrize(
    ("counted", "cause"), [(False, "time limit reached"), (True, "budget spent")]
)
async def test_a_stop_names_the_time_limit_when_it_came_first(
    caplog: pytest.LogCaptureFixture, counted: bool, cause: str
) -> None:
    """Past the deadline, the stop line names the time limit unless a count won."""
    from github_statistics.client import Budget, Transport
    from github_statistics.collect.steps import Steps

    budget = Budget(deadline=1.0, clock=lambda: 2.0)
    budget.stopped_by_count = counted
    async with Transport(None, sleep=_instant, budget=budget) as transport:
        with caplog.at_level(logging.INFO):
            Steps(transport).stop("backfill")

    messages = [r.getMessage() for r in caplog.records]
    assert f"{cause} in the backfill step; the next run resumes" in messages


async def test_a_step_stopped_past_the_deadline_names_the_time_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refusal that blames the budget is logged as the time limit it was."""
    from github_statistics.client import Budget, BudgetExhaustedError, Transport
    from github_statistics.collect.steps import Steps

    async def refused() -> None:
        raise BudgetExhaustedError("no GraphQL budget left for a query costing 1")

    budget = Budget(deadline=1.0, clock=lambda: 2.0)
    async with Transport(None, sleep=_instant, budget=budget) as transport:
        with caplog.at_level(logging.INFO):
            await Steps(transport).run("backfill", refused())

    messages = [r.getMessage() for r in caplog.records]
    assert "backfill step stopped: time limit reached" in messages
    assert not [m for m in messages if "budget" in m]


def _logged(caplog: pytest.LogCaptureFixture) -> set[tuple[str, str]]:
    """The steps and repositories with a progress line in ``caplog``."""
    return {
        (m.group(1), m.group(2))
        for r in caplog.records
        if (m := REPOSITORY.match(r.getMessage()))
    }


def test_stderr_respects_the_level_for_third_party_warnings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """At ``error``, a third-party warning reaches no stderr, even reconfigured."""
    from github_statistics.cli.app import LogLevel, configure_logging

    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    configure_logging(LogLevel.INFO)
    configure_logging(LogLevel.ERROR)
    logging.getLogger("httpx").warning("third-party warning")
    logging.getLogger("github_statistics").error("our error")

    err = capsys.readouterr().err
    assert "third-party warning" not in err
    assert "our error" in err
