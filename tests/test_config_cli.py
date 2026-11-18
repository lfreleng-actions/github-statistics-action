# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the ``config`` commands and the shared command line options."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

pytestmark = pytest.mark.xfail(strict=True, reason="config commands not implemented")

runner = CliRunner()

SECRET = "ghp_do_not_print_this_value"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's own configuration and token out of every test."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("XDG_CONFIG_HOME", "GHS_CONFIG", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _invoke(*args: str) -> Any:
    """Run the command line with ``args``."""
    from github_statistics.cli import app

    return runner.invoke(app, list(args))


def test_show_with_flags_alone_reports_cli_provenance() -> None:
    """The issue's acceptance command prints a valid configuration."""
    from github_statistics.config.schema import validate

    result = _invoke(
        "config", "show", "--org", "lfreleng-actions", "--org-bots", "a[bot], b[bot]"
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    validate(report["config"])
    assert report["source"] == "flags"
    [identities] = report["identities"]
    assert identities["owner"] == "lfreleng-actions"
    assert identities["org_bots"] == {"values": ["a[bot]", "b[bot]"], "source": "cli"}
    assert identities["member_orgs"]["source"] == "default"


def test_empty_identity_flag_counts_as_not_set() -> None:
    """``--org-bots ""`` leaves the configured list in force."""
    raw = json.dumps(
        {"identities": {"org_bots": ["x[bot]"]}, "targets": [{"owner": "a"}]}
    )

    result = _invoke("config", "show", "--config-data", raw, "--org-bots", "")

    assert result.exit_code == 0, result.output
    org_bots = json.loads(result.stdout)["identities"][0]["org_bots"]
    assert org_bots == {"values": ["x[bot]"], "source": "config"}


def test_identity_flag_repeats() -> None:
    """An identity flag may repeat; every occurrence counts."""
    result = _invoke(
        "config", "show", "--org", "a", "--members", "m1 m2", "--members", "m3"
    )

    members = json.loads(result.stdout)["identities"][0]["members"]
    assert members["values"] == ["m1", "m2", "m3"]


def test_show_never_prints_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither the environment's token nor a token in ``token_env`` is printed."""
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    raw = json.dumps({"targets": [{"owner": "a"}, {"owner": "b", "token_env": SECRET}]})

    result = _invoke("config", "show", "--config-data", raw)

    assert result.exit_code == 0, result.output
    assert SECRET not in result.output


def test_show_redacts_a_token_in_any_value() -> None:
    """A token-shaped value anywhere in a valid document is not printed."""
    raw = json.dumps(
        {
            "identities": {"members": [SECRET]},
            "targets": [{"owner": "a", "identities": {"org_bots": [SECRET]}}],
        }
    )

    result = _invoke("config", "show", "--config-data", raw)

    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    report = json.loads(result.stdout)
    assert report["identities"][0]["members"]["source"] == "config"


def test_show_redacts_a_whitespace_prefixed_token() -> None:
    """A token with surrounding whitespace is still redacted."""
    padded = f"  {SECRET}\n"
    raw = json.dumps(
        {
            "identities": {"members": [padded]},
            "targets": [{"owner": "a", "token_env": padded}],
        }
    )

    result = _invoke("config", "show", "--config-data", raw)

    assert result.exit_code == 0, result.output
    assert SECRET not in result.output


@pytest.mark.parametrize("command", ["show", "check"])
def test_invalid_configuration_never_prints_a_token(command: str) -> None:
    """A validation error does not echo a token from the rejected document."""
    raw = json.dumps({"targets": {"owner": "a", "token_env": SECRET}})

    result = _invoke("config", command, "--config-data", raw)

    assert result.exit_code == 1
    assert SECRET not in result.stdout + result.stderr


def test_show_reads_ghs_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """``$GHS_CONFIG`` is a source, and ``show`` names it."""
    monkeypatch.setenv("GHS_CONFIG", json.dumps({"targets": [{"owner": "env"}]}))

    report = json.loads(_invoke("config", "show").stdout)

    assert (report["source"], report["location"]) == ("env", "$GHS_CONFIG")


def test_check_passes_a_valid_configuration(tmp_path: Path) -> None:
    """``config check`` exits 0 and names the source."""
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"targets": [{"owner": "a"}]}))

    result = _invoke("config", "check", "--config", str(path))

    assert result.exit_code == 0
    assert str(path) in result.stdout


@pytest.mark.parametrize(
    "document",
    [
        {"targets": [{"owner": "a", "scan_private": True}]},
        {"targets": [{"owner": "a"}], "window": {"days": 1}},
    ],
)
def test_check_fails_with_one_line(document: dict[str, Any]) -> None:
    """An invalid configuration exits 1 with a one-line reason."""
    result = _invoke("config", "check", "--config-data", json.dumps(document))

    assert result.exit_code == 1
    assert result.stderr.startswith("error: ")
    assert result.stderr.count("\n") == 1


def test_check_without_configuration_fails() -> None:
    """With no source at all, ``check`` fails."""
    result = _invoke("config", "check")

    assert result.exit_code == 1
    assert "no configuration" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("--org", "a", "--include", "x", "--exclude", "y"),
        ("--repo", "a/b", "--include", "x"),
        ("--repo", "a/b", "--exclude", "y"),
    ],
)
def test_exclusive_flags_fail_at_parse_time(args: tuple[str, ...]) -> None:
    """``--include`` with ``--exclude``, and ``--repo`` with either, are usage errors."""
    result = _invoke("config", "show", *args)

    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_path_prints_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``config path`` prints the XDG default."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "conf"))

    result = _invoke("config", "path")

    assert result.stdout.strip() == str(tmp_path / "conf/github-statistics/config.json")
