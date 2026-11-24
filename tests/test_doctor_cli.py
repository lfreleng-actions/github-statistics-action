# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the ``doctor`` command (design §4.2)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

API = "https://api.github.com"
SECRET = "ghp_do_not_print_this_value"
UNTIL = date(2026, 3, 10)
BOT_KEYS = "identities.org_bots or identities.external_bots"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's configuration and cache out; set a token."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("GHS_CONFIG", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)


def _seed(tmp_path: Path) -> Path:
    """A store for ``acme``: scope, coverage, one run, members and two bots."""
    from github_statistics.collect import PULL_REQUESTS_METRIC
    from github_statistics.store import Item, Run, Store, store_path

    at = datetime(2026, 3, 4, 12, tzinfo=UTC)
    path = store_path(tmp_path / "cache", "api.github.com", "acme")
    with Store(path) as store:
        store.enter_scope("acme/a", date(2026, 3, 1))
        store.replace_members("acme", ["alice", "bob"], date(2026, 3, 1))
        store.replace_members("friends", ["carol"], date(2026, 3, 1))
        store.add_coverage("acme/a", PULL_REQUESTS_METRIC, date(2026, 3, 6), UNTIL)
        store.upsert_items(
            [
                Item("acme/a", n, f"PR_{n}", "pr", at, None, None, login, "Bot", at)
                for n, login in ((1, "renovate[bot]"), (2, "acme-ci[bot]"))
            ]
        )
        store.upsert_runs(
            [
                Run(
                    id=1,
                    node_id="WFR_1",
                    repo="acme/a",
                    workflow_path=".github/workflows/ci.yaml",
                    event="push",
                    status="completed",
                    conclusion="success",
                    created_at=at,
                    actor="alice",
                    actor_type="User",
                    attempt=1,
                    jobs=1,
                )
            ]
        )
    return path


def _doctor(tmp_path: Path, *extra: str) -> Any:
    """Run ``doctor`` for ``acme`` over the ten days to `UNTIL`."""
    from github_statistics.cli import app

    return runner.invoke(
        app,
        [
            "doctor",
            "--org",
            "acme",
            "--days",
            "10",
            "--until",
            UNTIL.isoformat(),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--member-orgs",
            "acme,friends",
            "--org-bots",
            "acme-ci[bot]",
            *extra,
        ],
    )


def _github(remaining: int = 4999) -> None:
    """Mock the rate limit and a listing of one public and one private repository."""
    core = {"limit": 5000, "remaining": remaining, "reset": 1_900_000_000}
    respx.get(f"{API}/rate_limit").respond(200, json={"resources": {"core": core}})
    respx.get(f"{API}/users/acme").respond(
        200, json={"login": "acme", "type": "Organization"}
    )
    repo = {
        "full_name": "acme/a",
        "private": False,
        "archived": False,
        "fork": False,
        "is_template": False,
        "created_at": "2020-01-02T03:04:05Z",
        "default_branch": "main",
    }
    respx.get(f"{API}/orgs/acme/repos").respond(
        200,
        json=[
            {**repo, "name": "a", "visibility": "public"},
            {**repo, "name": "s", "full_name": "acme/s", "visibility": "private"},
        ],
    )


def test_offline_reports_every_section_and_makes_no_request(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        result = _doctor(tmp_path, "--offline")
    assert result.exit_code == 0, result.output
    assert not mock.calls
    out = result.output
    assert "configuration: flags" in out
    assert "  org_bots: acme-ci[bot] (cli)" in out
    assert "  external_bots: none (default)" in out
    assert "  member_orgs: acme, friends (cli)" in out
    assert "token for acme: $GITHUB_TOKEN is set" in out
    assert "  rate limit: skipped (--offline)" in out
    assert "scope of acme: skipped (--offline)" in out
    assert str(path) in out
    assert "  runs: 1" in out
    assert "  pull_requests: 5 complete, 0 partial, 5 missing" in out
    assert "  workflows: 0 complete, 0 partial, 10 missing" in out
    assert "  workflow runs horizon: 2026-03-04" in out
    assert "  acme: 2" in out
    assert "  friends: 1" in out
    assert f"  renovate[bot]: add to {BOT_KEYS}" in out
    assert "acme-ci[bot]: add" not in out
    order = [
        "configuration:",
        "token for acme",
        "scope of acme",
        "cache:",
        "coverage of acme",
        "public members",
        "unclassified bots",
    ]
    assert [out.index(heading) for heading in order] == sorted(
        out.index(heading) for heading in order
    )


@respx.mock
def test_online_reports_rate_limit_and_dropped_repositories(tmp_path: Path) -> None:
    _seed(tmp_path)
    _github()
    result = _doctor(tmp_path)
    assert result.exit_code == 0, result.output
    assert "  rate limit: 4999 of 5000 left" in result.output
    assert "scope of acme: 1 repositories" in result.output
    assert "  selected acme/a" in result.output
    assert "  dropped acme/s: not public" in result.output
    assert not any("members" in str(call.request.url) for call in respx.calls)


@respx.mock
def test_the_token_value_never_appears(tmp_path: Path) -> None:
    _seed(tmp_path)
    _github()
    result = _doctor(tmp_path)
    assert result.exit_code == 0, result.output
    assert "GITHUB_TOKEN" in result.output
    assert SECRET not in result.output


def test_an_unset_token_is_reported_by_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")
    result = _doctor(tmp_path, "--offline")
    assert result.exit_code == 0, result.output
    assert "token for acme: $GITHUB_TOKEN is not set" in result.output


@respx.mock
def test_a_rejected_token_exits_three(tmp_path: Path) -> None:
    respx.get(f"{API}/rate_limit").respond(401, json={})
    result = _doctor(tmp_path)
    assert result.exit_code == 3
    assert "$GITHUB_TOKEN" in result.output
    assert SECRET not in result.output


def test_an_invalid_configuration_exits_four(tmp_path: Path) -> None:
    from github_statistics.cli import app

    result = runner.invoke(app, ["doctor", "--config-data", "{not json"])
    assert result.exit_code == 4
    assert "error:" in result.output


def test_a_missing_store_is_reported_without_a_traceback(tmp_path: Path) -> None:
    result = _doctor(tmp_path, "--offline")
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "no stores" in result.output
    assert "no store for acme" in result.output
    assert not (tmp_path / "cache").exists()


def test_a_corrupt_store_is_reported_and_the_rest_still_runs(tmp_path: Path) -> None:
    from github_statistics.store import store_path

    _seed(tmp_path)
    corrupt = store_path(tmp_path / "cache", "api.github.com", "other")
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not a database" * 100)
    result = _doctor(tmp_path, "--offline", "--org", "other")
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert f"cannot inspect the stores under {tmp_path / 'cache'}" in result.output
    assert f"cannot read the store for other at {corrupt}" in result.output
    assert "coverage of acme" in result.output
    assert "unclassified bots of acme: 1" in result.output


@respx.mock
def test_scope_reads_keep_the_reserve_and_report_a_spent_budget(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    _github(remaining=100)
    result = _doctor(tmp_path)
    assert result.exit_code == 0, result.output
    assert "  rate limit: 100 of 5000 left" in result.output
    assert "scope of acme: budget spent" in result.output
    assert [call.request.url.path for call in respx.calls] == ["/rate_limit"]


@respx.mock
def test_scope_reads_stop_at_the_configured_budget(tmp_path: Path) -> None:
    _seed(tmp_path)
    _github()
    result = _doctor(tmp_path, "--budget", "1")
    assert result.exit_code == 0, result.output
    assert "scope of acme: budget spent" in result.output
    paths = [call.request.url.path for call in respx.calls]
    assert paths == ["/rate_limit", "/users/acme"]


@respx.mock
def test_scope_is_skipped_without_a_rate_limit_count(tmp_path: Path) -> None:
    _seed(tmp_path)
    respx.get(f"{API}/rate_limit").respond(200, json={})
    result = _doctor(tmp_path)
    assert result.exit_code == 0, result.output
    assert "  rate limit: unknown" in result.output
    assert "scope of acme: skipped (no rate limit count" in result.output
    assert [call.request.url.path for call in respx.calls] == ["/rate_limit"]
    assert "coverage of acme" in result.output


@respx.mock
def test_a_stored_repository_turned_private_is_reported(tmp_path: Path) -> None:
    from github_statistics.store import Store

    with Store(_seed(tmp_path)) as store:
        store.enter_scope("acme/hidden", date(2026, 3, 1))
    _github()
    respx.get(f"{API}/repos/acme/hidden").respond(
        200,
        json={
            "name": "hidden",
            "full_name": "acme/hidden",
            "private": True,
            "visibility": "private",
        },
    )
    result = _doctor(tmp_path)
    assert result.exit_code == 0, result.output
    assert "  dropped acme/hidden: not public" in result.output


def test_a_store_failing_a_health_query_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import importlib
    import sqlite3

    doctor = importlib.import_module("github_statistics.cli.doctor")

    def damaged(*_args: object) -> None:
        raise sqlite3.DatabaseError("database disk image is malformed")

    path = _seed(tmp_path)
    monkeypatch.setattr(doctor, "runs_horizon", damaged)
    result = _doctor(tmp_path, "--offline")
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert f"cannot read the store for acme at {path}: database disk" in result.output


@respx.mock
def test_a_pasted_token_is_redacted(tmp_path: Path) -> None:
    pasted = "ghp_pasted_into_the_configuration"
    respx.get(f"{API}/rate_limit").respond(401, json={})
    _seed(tmp_path)
    result = _doctor(tmp_path, "--token-env", pasted, "--external-bots", pasted)
    assert result.exit_code == 3
    assert pasted not in result.output
    assert "token for acme: $<redacted: looks like a token> is not set" in result.output
    assert "  external_bots: <redacted: looks like a token> (cli)" in result.output
    assert "rejected the token in $<redacted: looks like a token>" in result.output


def test_a_pasted_member_org_is_redacted(tmp_path: Path) -> None:
    pasted = "ghp_pasted_into_the_configuration"
    _seed(tmp_path)
    result = _doctor(tmp_path, "--offline", "--member-orgs", f"acme,{pasted}")
    assert result.exit_code == 0, result.output
    assert pasted not in result.output
    assert "  <redacted: looks like a token>: 0" in result.output
