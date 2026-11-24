# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the data export and the ``export`` command (design §8.6)."""

from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import respx
from typer.testing import CliRunner

runner = CliRunner()

UNTIL = date(2026, 3, 10)
GENERATED = date(2026, 3, 11)
SECRET = "ghp_do_not_print_this_value"
FILES = ("contributors", "coverage", "meta", "repositories", "reviews", "series")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's own configuration and cache out of every test."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("GHS_CONFIG", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)


def _at(day: int, hour: int = 12) -> datetime:
    """A UTC timestamp on ``day`` March 2026."""
    return datetime(2026, 3, day, hour, tzinfo=UTC)


def _fill(store: Any) -> None:
    """One repository in scope from 1 March, with a pull request, review and run."""
    from github_statistics.collect import PULL_REQUESTS_METRIC
    from github_statistics.store import Item, Review, Run

    day = date(2026, 3, 1)
    store.enter_scope("acme/a", day)
    store.replace_members("acme", ["alice"], day)
    store.add_coverage("acme/a", PULL_REQUESTS_METRIC, day, date(2026, 3, 5))
    store.upsert_items(
        [
            Item(
                repo="acme/a",
                number=1,
                node_id="PR_1",
                kind="pr",
                created_at=_at(2),
                closed_at=_at(3),
                merged_at=_at(3),
                author="alice",
                author_type="User",
                updated_at=_at(3),
            )
        ]
    )
    store.replace_reviews(
        "acme/a",
        1,
        [
            Review(1, "PRR_1", "acme/a", 1, "rêviewer", "User", "APPROVED", _at(3)),
            Review(2, "PRR_2", "acme/a", 1, "dependabot", "Bot", "COMMENTED", _at(3)),
            Review(3, "PRR_3", "acme/a", 1, "skip-me", "User", "COMMENTED", _at(3)),
        ],
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
                created_at=_at(4),
                actor="alice",
                actor_type="User",
                attempt=1,
                jobs=2,
            )
        ]
    )


def _config() -> Any:
    """A ten-day configuration for ``acme`` that ignores ``skip-me``."""
    from github_statistics.config import Config, Identities, Target
    from github_statistics.config.models import Window

    return Config(
        targets=(Target("acme", include=("a*",)),),
        window=Window(days=10),
        identities=Identities(ignore_logins=("skip-me",)),
    )


def _documents() -> dict[str, Any]:
    """The export documents of a filled in-memory store."""
    from github_statistics.export import build_documents
    from github_statistics.store import Store

    config = _config()
    with Store(Path(":memory:")) as store:
        _fill(store)
        return build_documents(
            store,
            config,
            config.targets[0],
            until=UNTIL,
            generated=GENERATED,
            version="1.2.3",
        )


def test_documents_cover_every_file() -> None:
    assert sorted(_documents()) == list(FILES)


def test_series_document_holds_day_values_and_coverage() -> None:
    series = _documents()["series"]
    prs = series["metrics"]["pull_requests"]
    opened = prs["series"]["opened"]["member"]
    assert len(opened) == 10
    assert opened[1] == {"day": "2026-03-02", "value": 1}
    assert set(prs["series"]["opened"]) >= {"all", "humans", "bots", "org_bot"}
    assert prs["coverage"][0] == {"day": "2026-03-01", "coverage": "complete"}
    assert prs["coverage"][5]["coverage"] == "none"


def test_coverage_document_lists_days_by_grade_and_the_runs_horizon() -> None:
    coverage = _documents()["coverage"]
    assert coverage["runs_horizon"] == "2026-03-04"
    prs = coverage["metrics"]["pull_requests"]
    assert prs["complete"] == [f"2026-03-0{day}" for day in range(1, 6)]
    assert prs["partial"] == []
    assert len(prs["missing"]) == 5


def test_tables_and_reviews_documents() -> None:
    documents = _documents()
    [alice, *_] = documents["contributors"]["contributors"]
    assert alice["login"] == "alice"
    assert alice["class"] == "member"
    assert alice["prs_merged"] == 1
    [repo] = documents["repositories"]["repositories"]
    assert repo["repository"] == "acme/a"
    assert repo["coverage"]["pull_requests"] == 0.5
    reviews = documents["reviews"]["reviews"]
    assert reviews == [
        {
            "repository": "acme/a",
            "number": 1,
            "reviewer": "rêviewer",
            "class": "external",
            "state": "APPROVED",
            "submitted_at": "2026-03-03T12:00:00Z",
        },
        {
            "repository": "acme/a",
            "number": 1,
            "reviewer": "dependabot[bot]",
            "class": "external_bot",
            "state": "COMMENTED",
            "submitted_at": "2026-03-03T12:00:00Z",
        },
    ]


def test_meta_document_records_the_run() -> None:
    meta = _documents()["meta"]
    assert meta["tool_version"] == "1.2.3"
    assert meta["target"] == "acme"
    assert meta["window"] == {"days": 10, "from": "2026-03-01", "until": "2026-03-10"}
    assert meta["generated"] == "2026-03-11"
    assert meta["scope_rule"]["include"] == ["a*"]
    assert meta["unclassified_bots"] == ["dependabot"]
    assert meta["identities"]["ignore_logins"] == {
        "values": ["skip-me"],
        "source": "config",
    }


def test_json_files_are_compact_and_deterministic(tmp_path: Path) -> None:
    from github_statistics.export import write_json

    first = write_json(_documents(), tmp_path / "one")
    second = write_json(_documents(), tmp_path / "two")
    assert [path.name for path in first] == [f"{name}.json" for name in FILES]
    for one, two in zip(first, second, strict=True):
        text = one.read_text(encoding="utf-8")
        assert one.read_bytes() == two.read_bytes()
        assert "\n" not in text
        assert ": " not in text and ", " not in text
        assert text == json.dumps(
            json.loads(text), separators=(",", ":"), sort_keys=True, ensure_ascii=False
        )
    assert "rêviewer" in (tmp_path / "one" / "reviews.json").read_text("utf-8")


def test_csv_writes_one_file_per_table(tmp_path: Path) -> None:
    from github_statistics.export import write_csv

    paths = write_csv(_documents(), tmp_path)
    assert [path.name for path in paths] == [
        "contributors.csv",
        "coverage.csv",
        "repositories.csv",
        "reviews.csv",
        "series.csv",
    ]
    with (tmp_path / "series.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["metric", "series", "class", "day", "value"]
    assert ["pull_requests", "opened", "member", "2026-03-02", "1"] in rows
    with (tmp_path / "repositories.csv").open(encoding="utf-8", newline="") as handle:
        header = next(csv.reader(handle))
    assert header[0] == "repository"
    assert "coverage_pull_requests" in header


def _seed(tmp_path: Path) -> Path:
    """A filled store where ``export`` looks for ``acme``'s under ``tmp_path``."""
    from github_statistics.store import Store, store_path

    path = store_path(tmp_path / "cache", "api.github.com", "acme")
    with Store(path) as store:
        _fill(store)
    return path


def _export(tmp_path: Path, *extra: str) -> Any:
    """Run ``export`` for ``acme`` over the ten days to `UNTIL`."""
    from github_statistics.cli import app

    return runner.invoke(
        app,
        [
            "export",
            "--org",
            "acme",
            "--days",
            "10",
            "--until",
            UNTIL.isoformat(),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output-dir",
            str(tmp_path / "site"),
            *extra,
        ],
    )


@pytest.mark.parametrize(("flag", "suffix"), [("json", "json"), ("csv", "csv")])
def test_export_command_reads_the_store_and_makes_no_request(
    tmp_path: Path, flag: str, suffix: str
) -> None:
    _seed(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        result = _export(tmp_path, "--format", flag)
    assert result.exit_code == 0, result.output
    assert not mock.calls
    assert SECRET not in result.output
    data = tmp_path / "site" / "acme" / "data"
    assert (data / f"series.{suffix}").is_file()
    assert str(data / f"contributors.{suffix}") in result.output


def test_export_command_fails_without_a_store(tmp_path: Path) -> None:
    result = _export(tmp_path)
    assert result.exit_code == 1
    assert "no store for acme" in result.output
    assert not (tmp_path / "cache").exists()


def test_export_command_refuses_a_stale_store_and_leaves_it_alone(
    tmp_path: Path,
) -> None:
    from github_statistics.store import SCHEMA_VERSION

    path = _seed(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    result = _export(tmp_path)
    assert result.exit_code == 1
    assert "is stale" in result.output
    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        items = connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert (version, items) == (SCHEMA_VERSION + 1, 1)


def test_read_only_store_never_creates_a_file(tmp_path: Path) -> None:
    from github_statistics.cli.export import ReadOnlyStore

    path = tmp_path / "gone" / "store.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        ReadOnlyStore(path)
    assert not path.parent.exists()


def test_read_only_store_refuses_writes(tmp_path: Path) -> None:
    from github_statistics.cli.export import ReadOnlyStore

    with ReadOnlyStore(_seed(tmp_path)) as store, pytest.raises(sqlite3.Error):
        store.enter_scope("acme/b", date(2026, 3, 1))


def _delete_items(path: Path) -> bool:
    """Try to delete every item from another connection; whether it committed."""
    writer = sqlite3.connect(path, timeout=0)
    try:
        writer.execute("DELETE FROM items")
        writer.commit()
    except sqlite3.OperationalError:
        return False
    finally:
        writer.close()
    return True


def test_read_only_store_reads_one_snapshot_from_open_to_close(
    tmp_path: Path,
) -> None:
    from github_statistics.cli.export import ReadOnlyStore

    path = _seed(tmp_path)
    with ReadOnlyStore(path) as store:
        _delete_items(path)
        items = store.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert items == 1
    assert _delete_items(path)


def test_cli_starts_without_loading_matplotlib() -> None:
    code = (
        "import sys, github_statistics.cli.export; print('matplotlib' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"
