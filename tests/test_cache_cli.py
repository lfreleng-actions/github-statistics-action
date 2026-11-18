# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the ``cache`` commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the default cache and configuration into the test's own directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("GHS_CONFIG", raising=False)


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


def test_info_on_an_empty_cache(tmp_path: Path) -> None:
    """``cache info`` succeeds when no store exists yet."""
    result = _invoke("cache", "info")

    assert result.exit_code == 0, result.output
    assert str(tmp_path / "xdg" / "github-statistics") in result.output
    assert "no stores" in result.output


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


def test_clear_by_owner_and_all(tmp_path: Path) -> None:
    """``cache clear --owner`` removes one owner's store; plain clear removes all."""
    acme = _make_store(tmp_path, "acme")
    other = _make_store(tmp_path, "other")
    keep = tmp_path / "unrelated.txt"
    keep.write_text("mine")

    for hostile in ("..", ""):
        result = _invoke(
            "cache", "clear", "--cache-dir", str(tmp_path), "--owner", hostile
        )

        assert result.exit_code == 1
        assert acme.exists()
        assert other.exists()

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


def test_clear_skips_symlinks_out_of_the_cache(tmp_path: Path) -> None:
    """``cache info`` and ``clear`` skip every link, a self-referential one too."""
    cache = tmp_path / "cache"
    cache.mkdir()
    outside = tmp_path / "outside"
    host = _make_store(outside, "acme")
    owner = _make_store(outside / "elsewhere", "acme")
    store = _make_store(outside / "file", "acme")
    (cache / "api.github.com").symlink_to(host.parent.parent)
    (cache / "ghe.example.com").mkdir()
    (cache / "ghe.example.com" / "acme").symlink_to(owner.parent)
    (cache / "ghe2.example.com" / "acme").mkdir(parents=True)
    (cache / "ghe2.example.com" / "acme" / store.name).symlink_to(store)
    loop = cache / "loop.example.com" / "acme" / store.name
    loop.parent.mkdir(parents=True)
    loop.symlink_to(loop)
    real = _make_store(tmp_path / "staging", "acme").parent.parent
    real = real.rename(cache / "real.example.com") / "acme" / store.name

    info = _invoke("cache", "info", "--cache-dir", str(cache))
    result = _invoke("cache", "clear", "--cache-dir", str(cache), "--owner", "acme")

    assert info.exit_code == 0, info.output
    assert result.exit_code == 0, result.output
    assert info.output.count(store.name) == 1
    assert str(real) in info.output
    assert host.exists()
    assert owner.exists()
    assert store.exists()
    assert (cache / "api.github.com").is_symlink()
    assert (cache / "ghe.example.com" / "acme").is_symlink()
    assert (cache / "ghe2.example.com" / "acme" / store.name).is_symlink()
    assert loop.is_symlink()
    assert not real.exists()


def test_clear_keeps_linked_side_files(tmp_path: Path) -> None:
    """``cache clear`` removes a store's regular side files but no link."""
    cache = tmp_path / "cache"
    store = _make_store(cache, "acme")
    target = tmp_path / "outside-wal"
    target.write_text("mine")
    wal = store.with_name(store.name + "-wal")
    wal.symlink_to(target)
    shm = store.with_name(store.name + "-shm")
    shm.symlink_to(tmp_path / "missing")
    journal = store.with_name(store.name + "-journal")
    journal.write_text("")

    result = _invoke("cache", "clear", "--cache-dir", str(cache))

    assert result.exit_code == 0, result.output
    assert not store.exists()
    assert not journal.exists()
    assert wal.is_symlink()
    assert shm.is_symlink()
    assert target.read_text() == "mine"


def _config(directory: Path) -> str:
    """A configuration document whose ``cache.directory`` is ``directory``."""
    return json.dumps(
        {"targets": [{"owner": "acme"}], "cache": {"directory": str(directory)}}
    )


def test_commands_use_the_configured_cache_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ``--cache-dir``, the default file's or ``$GHS_CONFIG``'s directory wins."""
    configured = _make_store(tmp_path / "configured", "acme")
    fallback = _make_store(tmp_path / "xdg" / "github-statistics", "acme")
    config_file = tmp_path / "config" / "github-statistics" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(_config(tmp_path / "configured"))

    result = _invoke("cache", "info")

    assert result.exit_code == 0, result.output
    assert str(configured) in result.output
    assert str(fallback) not in result.output

    monkeypatch.setenv("GHS_CONFIG", _config(tmp_path / "from-env"))
    assert str(tmp_path / "from-env") in _invoke("cache", "info").output
    monkeypatch.delenv("GHS_CONFIG")

    result = _invoke("cache", "clear")

    assert result.exit_code == 0, result.output
    assert not configured.exists()
    assert fallback.exists()


@pytest.mark.parametrize("command", ["info", "clear"])
def test_an_empty_cache_dir_is_an_error(tmp_path: Path, command: str) -> None:
    """An explicit empty ``--cache-dir`` exits 1 and falls back to no other cache."""
    configured = _make_store(tmp_path / "configured", "acme")
    fallback = _make_store(tmp_path / "xdg" / "github-statistics", "acme")

    result = _invoke(
        "cache",
        command,
        "--cache-dir",
        "",
        "--config-data",
        _config(configured.parents[2]),
    )

    assert result.exit_code == 1
    assert "--cache-dir" in result.output
    assert configured.exists()
    assert fallback.exists()


@pytest.mark.parametrize("flag", ["--config", "--config-data"])
@pytest.mark.parametrize("command", ["info", "clear"])
def test_an_empty_configuration_flag_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str, flag: str
) -> None:
    """An explicit empty configuration flag exits 1 and falls back to no cache."""
    configured = _make_store(tmp_path / "configured", "acme")
    fallback = _make_store(tmp_path / "xdg" / "github-statistics", "acme")
    monkeypatch.setenv("GHS_CONFIG", _config(configured.parents[2]))

    result = _invoke("cache", command, flag, "")

    assert result.exit_code == 1
    assert flag in result.output
    assert "cache:" not in result.output
    assert configured.exists()
    assert fallback.exists()


@pytest.mark.parametrize("command", ["info", "clear"])
def test_a_malformed_configuration_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    """A configuration that does not validate exits 1 and clears nothing."""
    fallback = _make_store(tmp_path / "xdg" / "github-statistics", "acme")
    monkeypatch.setenv("GHS_CONFIG", '{"cache": {"directory": 1}}')

    result = _invoke("cache", command)

    assert result.exit_code == 1
    assert "GHS_CONFIG" in result.output
    assert fallback.exists()
