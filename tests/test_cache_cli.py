# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the ``cache`` commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the default cache directory into the test's own directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))


def _invoke(*args: str) -> Any:
    """Run the command line with ``args``."""
    from github_statistics.cli import app

    return runner.invoke(app, list(args))


def _make_store(cache_dir: Path, owner: str) -> Path:
    """Create a store holding one scope row for ``owner``."""
    from datetime import date

    from github_statistics.store import Store, store_path

    path = store_path(cache_dir, "api.github.com", owner)
    with Store(path) as store:
        store.enter_scope(f"{owner}/a", date(2026, 3, 1))
    return path


@pytest.mark.xfail(strict=True, reason="cache commands not implemented")
def test_info_on_an_empty_cache(tmp_path: Path) -> None:
    """``cache info`` succeeds when no store exists yet."""
    result = _invoke("cache", "info")

    assert result.exit_code == 0, result.output
    assert str(tmp_path / "xdg" / "github-statistics") in result.output
    assert "no stores" in result.output


@pytest.mark.xfail(strict=True, reason="cache commands not implemented")
def test_info_reports_each_store(tmp_path: Path) -> None:
    """``cache info`` names each store with its version and row counts."""
    from github_statistics.store import SCHEMA_VERSION

    path = _make_store(tmp_path, "acme")

    result = _invoke("cache", "info", "--cache-dir", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert str(path) in result.output
    assert f"schema version: {SCHEMA_VERSION}" in result.output
    assert "scope: 1" in result.output
    assert "items: 0" in result.output


@pytest.mark.xfail(strict=True, reason="cache commands not implemented")
def test_info_does_not_rebuild_a_stale_store(tmp_path: Path) -> None:
    """``cache info`` reads a store without changing it."""
    import sqlite3

    path = _make_store(tmp_path, "acme")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 999")
    connection.commit()
    connection.close()

    result = _invoke("cache", "info", "--cache-dir", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert "schema version: 999" in result.output
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM scope").fetchone() == (1,)
    connection.close()


@pytest.mark.xfail(strict=True, reason="cache commands not implemented")
def test_clear_by_owner_and_all(tmp_path: Path) -> None:
    """``cache clear --owner`` removes one owner's store; plain clear removes all."""
    acme = _make_store(tmp_path, "acme")
    other = _make_store(tmp_path, "other")
    keep = tmp_path / "unrelated.txt"
    keep.write_text("mine")

    result = _invoke("cache", "clear", "--cache-dir", str(tmp_path), "--owner", "ACME")

    assert result.exit_code == 0, result.output
    assert not acme.exists()
    assert other.exists()

    result = _invoke("cache", "clear", "--cache-dir", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert not other.exists()
    assert keep.read_text() == "mine"
    assert (
        "nothing to clear"
        in _invoke("cache", "clear", "--cache-dir", str(tmp_path)).output
    )
