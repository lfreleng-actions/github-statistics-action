# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the charts of a target and the ``render`` command (design §8.4)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

MEMORY = Path(":memory:")
UNTIL = date(2026, 3, 10)
NOW = datetime(2026, 3, 11, 6, 30, tzinfo=UTC)
VIEWS = ("all", "member", "external", "org-bot", "external-bot")
DEFAULT_METRICS = ("pull-requests", "issues", "workflows")

runner = CliRunner()


def _day(day: int) -> date:
    """``day`` March 2026."""
    return date(2026, 3, day)


def _config(**changes: Any) -> Any:
    """A small-chart configuration for ``acme`` over ten days."""
    from github_statistics.config import Config, Target
    from github_statistics.config.models import Render, Window

    values: dict[str, Any] = {
        "targets": (Target("acme"),),
        "window": Window(days=10),
        "render": Render(width=384, height=216, derivatives=(192,)),
    }
    values.update(changes)
    return Config(**values)


def _fill(store: Any) -> None:
    """Two repositories in scope from 1 March, one pull request and one run."""
    from github_statistics.store import Item, Run

    for repo in ("acme/a", "acme/b"):
        store.enter_scope(repo, _day(1))
    store.add_coverage("acme/a", "pull_requests", _day(1), _day(9))
    store.add_coverage("acme/b", "pull_requests", _day(1), _day(8))
    at = datetime(2026, 3, 2, 12, tzinfo=UTC)
    store.upsert_items(
        [
            Item(
                repo="acme/a",
                number=1,
                node_id="PR_1",
                kind="pr",
                created_at=at,
                closed_at=None,
                merged_at=None,
                author="alice",
                author_type="User",
                updated_at=at,
            )
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
                jobs=3,
            )
        ]
    )


def _render(tmp_path: Path, config: Any = None, now: datetime = NOW) -> list[Any]:
    """Build the series of ``acme`` from a hand-built store and render them."""
    from github_statistics.render.charts import render_target
    from github_statistics.series import build_series
    from github_statistics.store import Store

    config = config or _config()
    target = config.targets[0]
    with Store(MEMORY) as store:
        _fill(store)
        series = build_series(store, config, target, until=UNTIL)
    return render_target(series, config, target, tmp_path, now=now)


def _names(charts: list[Any]) -> list[str]:
    """The master file names of ``charts``."""
    return [chart.chart.path.name for chart in charts]


@pytest.mark.xfail(strict=True)
def test_default_configuration_writes_fifteen_masters_and_derivatives(
    tmp_path: Path,
) -> None:
    charts = _render(tmp_path)

    expected = [f"{metric}-{view}.png" for metric in DEFAULT_METRICS for view in VIEWS]
    assert _names(charts) == expected
    images = tmp_path / "acme" / "img"
    assert sorted(path.name for path in images.iterdir()) == sorted(
        expected + [name.replace(".png", "-192.png") for name in expected]
    )


@pytest.mark.xfail(strict=True)
def test_series_pairs_follow_the_metric(tmp_path: Path) -> None:
    from github_statistics.config.models import Metrics

    charts = _render(
        tmp_path, _config(metrics=Metrics(workflow_outcomes=True), classes=("all",))
    )

    pairs = [(chart.spec.first.key, chart.spec.second.key) for chart in charts]
    assert pairs == [
        ("opened", "closed"),
        ("opened", "closed"),
        ("runs", "jobs"),
        ("succeeded", "failed"),
    ]
    assert [chart.spec.back_on_tie for chart in charts] == [
        "closed",
        "closed",
        "jobs",
        "failed",
    ]
    assert charts[0].spec.first.values == (0, 1, 0, 0, 0, 0, 0, 0, 0, 0)


@pytest.mark.xfail(strict=True)
def test_disabled_metric_renders_nothing(tmp_path: Path) -> None:
    from github_statistics.config.models import Metrics

    charts = _render(tmp_path, _config(metrics=Metrics(issues=False)))

    assert len(charts) == 10
    assert not any(name.startswith("issues-") for name in _names(charts))


@pytest.mark.xfail(strict=True)
def test_classes_reduced_to_all(tmp_path: Path) -> None:
    charts = _render(tmp_path, _config(classes=("all",)))

    assert _names(charts) == [f"{metric}-all.png" for metric in DEFAULT_METRICS]


@pytest.mark.xfail(strict=True)
def test_palette_and_back_on_tie_overrides_reach_the_spec(tmp_path: Path) -> None:
    from types import MappingProxyType

    from github_statistics.config.models import BackOnTie, Render

    render = Render(
        width=384,
        height=216,
        derivatives=(),
        palette=MappingProxyType({"opened": "#112233", "jobs": "#445566"}),
        back_on_tie=BackOnTie(pull_requests="opened", workflows="runs"),
    )
    charts = _render(tmp_path, _config(render=render, classes=("all",)))

    pulls, issues, workflows = (chart.spec for chart in charts)
    assert pulls.first.colour == "#112233"
    assert pulls.second.colour == "#E69F00"
    assert pulls.back_on_tie == "opened"
    assert issues.back_on_tie == "closed"
    assert workflows.second.colour == "#445566"
    assert workflows.back_on_tie == "runs"


@pytest.mark.xfail(strict=True)
def test_title_subtitle_and_footer_for_a_known_store(tmp_path: Path) -> None:
    from github_statistics import __version__

    charts = _render(tmp_path, _config(classes=("all", "org_bot")))

    spec = charts[0].spec
    assert spec.title == "acme — pull requests per day"
    assert spec.subtitle == (
        "10 days to 2026-03-10 (UTC) · 2 repositories · everyone · opened vs closed"
    )
    assert spec.footer == (
        "Coverage: 8 of 10 days complete · 1 day without data"
        f" · github-statistics {__version__} · generated 2026-03-11"
    )
    assert "our bots" in charts[1].spec.subtitle
    assert [chart.spec.title for chart in charts[::2]] == [
        "acme — pull requests per day",
        "acme — issues per day",
        "acme — workflow runs and jobs per day",
    ]


@pytest.mark.xfail(strict=True)
def test_coverage_goes_to_the_renderer_unchanged(tmp_path: Path) -> None:
    charts = _render(tmp_path, _config(classes=("all",)))

    letters = "".join(str(day)[0].upper() for day in charts[0].spec.coverage)
    assert letters == "CCCCCCCCPN"
    assert charts[0].chart.gap_slabs > 0


@pytest.mark.xfail(strict=True)
def test_single_repository_target_names_the_repository(tmp_path: Path) -> None:
    from github_statistics.config import Target

    charts = _render(
        tmp_path, _config(targets=(Target("acme", include=("a",)),), classes=("all",))
    )

    assert charts[0].spec.title == "acme/a — pull requests per day"


@pytest.mark.xfail(strict=True)
def test_derivatives_take_each_configured_width(tmp_path: Path) -> None:
    from PIL import Image

    from github_statistics.config.models import Render

    render = Render(width=384, height=216, derivatives=(96, 192))
    charts = _render(tmp_path, _config(render=render, classes=("all",)))

    master = charts[0]
    assert [path.name for path in master.derivatives] == [
        "pull-requests-all-96.png",
        "pull-requests-all-192.png",
    ]
    with Image.open(master.chart.path) as image:
        assert image.size == (384, 216)
    widths = []
    for path in master.derivatives:
        with Image.open(path) as image:
            widths.append(image.width)
    assert widths == [96, 192]


@pytest.mark.xfail(strict=True)
def test_svg_output_is_refused_before_any_file_is_written(tmp_path: Path) -> None:
    from github_statistics.config.models import Render

    render = Render(width=384, height=216, derivatives=(192,), svg=True)
    with pytest.raises(ValueError, match=r"render\.svg"):
        _render(tmp_path, _config(render=render, classes=("all",)))

    assert not (tmp_path / "acme").exists()


@pytest.mark.xfail(strict=True)
def test_oversized_derivative_is_refused_before_any_file_is_written(
    tmp_path: Path,
) -> None:
    from github_statistics.config.models import Render

    render = Render(width=384, height=216, derivatives=(192, 768))
    with pytest.raises(ValueError, match=r"render\.derivatives.*768"):
        _render(tmp_path, _config(render=render, classes=("all",)))

    assert not (tmp_path / "acme").exists()


@pytest.mark.xfail(strict=True)
def test_two_renders_with_the_same_now_are_byte_identical(tmp_path: Path) -> None:
    config = _config(classes=("all",))
    first = _render(tmp_path / "one", config)
    second = _render(tmp_path / "two", config, now=NOW.replace(hour=23))

    for left, right in zip(first, second, strict=True):
        assert left.chart.path.read_bytes() == right.chart.path.read_bytes()


@pytest.fixture
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the default cache and configuration into the test's own directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("GHS_CONFIG", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def _invoke(*args: str) -> Any:
    """Run the command line with ``args``."""
    from github_statistics.cli import app

    return runner.invoke(app, list(args))


@pytest.mark.xfail(strict=True)
@pytest.mark.usefixtures("_isolated")
def test_render_command_without_a_store_says_run_collect(tmp_path: Path) -> None:
    result = _invoke(
        "render", "--org", "example", "--cache-dir", str(tmp_path / "cache")
    )

    assert result.exit_code == 1
    assert "run `collect` first" in result.output
    assert not (tmp_path / "cache").exists()


@pytest.mark.xfail(strict=True)
@pytest.mark.usefixtures("_isolated")
def test_render_command_reads_the_store_and_lists_the_files(tmp_path: Path) -> None:
    from github_statistics.store import Store, store_path

    cache = tmp_path / "cache"
    path = store_path(cache, "api.github.com", "example")
    with Store(path) as store:
        store.enter_scope("example/a", _day(1))
    before = path.read_bytes()
    config = (
        '{"targets": [{"owner": "example"}],'
        ' "render": {"width": 384, "height": 216, "derivatives": [192]}}'
    )

    result = _invoke(
        "render",
        "--config-data",
        config,
        "--cache-dir",
        str(cache),
        "--output-dir",
        str(tmp_path / "site"),
        "--days",
        "10",
        "--until",
        "2026-03-10",
    )

    assert result.exit_code == 0, result.output
    written = result.output.split()
    assert len(written) == 30
    assert all(Path(name).is_file() for name in written)
    assert str(tmp_path / "site" / "example" / "img" / "issues-all.png") in written
    assert path.read_bytes() == before


@pytest.mark.xfail(strict=True)
@pytest.mark.usefixtures("_isolated")
def test_render_command_builds_the_series_in_one_read_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import github_statistics.series as series
    from github_statistics.store import Store, store_path

    cache = tmp_path / "cache"
    with Store(store_path(cache, "api.github.com", "example")) as store:
        store.enter_scope("example/a", _day(1))
    build = series.build_series
    seen: list[bool] = []

    def spy(store: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(store.connection.in_transaction)
        return build(store, *args, **kwargs)

    monkeypatch.setattr(series, "build_series", spy)
    config = (
        '{"targets": [{"owner": "example"}], "classes": ["all"],'
        ' "render": {"width": 384, "height": 216, "derivatives": []}}'
    )

    result = _invoke(
        "render",
        "--config-data",
        config,
        "--cache-dir",
        str(cache),
        "--output-dir",
        str(tmp_path / "site"),
        "--days",
        "10",
        "--until",
        "2026-03-10",
    )

    assert result.exit_code == 0, result.output
    assert seen == [True]


def _two_repository_store(
    cache: Path, *, b_entered: date = date(2026, 2, 1), b_left: date | None = None
) -> None:
    """``example/a`` and ``example/b`` in scope, one pull request in each."""
    from github_statistics.store import Item, Store, store_path

    with Store(store_path(cache, "api.github.com", "example")) as store:
        for number, repo in enumerate(("example/a", "example/b"), start=1):
            store.enter_scope(repo, date(2026, 2, 1) if number == 1 else b_entered)
            at = datetime(2026, 3, 2 + number, 12, tzinfo=UTC)
            store.upsert_items(
                [
                    Item(
                        repo=repo,
                        number=number,
                        node_id=f"PR_{number}",
                        kind="pr",
                        created_at=at,
                        closed_at=None,
                        merged_at=None,
                        author="alice",
                        author_type="User",
                        updated_at=at,
                    )
                ]
            )
        if b_left:
            store.leave_scope("example/b", b_left)


def _invoke_selected(tmp_path: Path, *selection: str) -> Any:
    """Render ``example`` from ``tmp_path``'s cache with ``selection``."""
    config = (
        '{"targets": [{"owner": "example"}], "classes": ["all"],'
        ' "render": {"width": 384, "height": 216, "derivatives": []}}'
    )
    return _invoke(
        "render",
        "--config-data",
        config,
        *selection,
        "--cache-dir",
        str(tmp_path / "cache"),
        "--output-dir",
        str(tmp_path / "site"),
        "--days",
        "10",
        "--until",
        "2026-03-10",
    )


@pytest.mark.xfail(strict=True)
@pytest.mark.usefixtures("_isolated")
@pytest.mark.parametrize(
    "selection", [("--repo", "example/a"), ("--exclude", "b"), ("--include", "a*")]
)
def test_render_command_refuses_a_selection_the_store_cannot_honour(
    tmp_path: Path, selection: tuple[str, ...]
) -> None:
    _two_repository_store(tmp_path / "cache")

    result = _invoke_selected(tmp_path, *selection)

    assert result.exit_code == 1
    assert "holds example/b in the window, outside the repository selection" in (
        result.output
    )
    assert not (tmp_path / "site").exists()


@pytest.mark.xfail(strict=True)
@pytest.mark.usefixtures("_isolated")
def test_render_command_accepts_a_selection_matching_the_window_scope(
    tmp_path: Path,
) -> None:
    _two_repository_store(tmp_path / "cache", b_left=date(2026, 2, 20))

    result = _invoke_selected(tmp_path, "--repo", "example/a")

    assert result.exit_code == 0, result.output
    assert len(result.output.split()) == 3


@pytest.mark.xfail(strict=True)
@pytest.mark.usefixtures("_isolated")
def test_render_command_ignores_a_repository_in_scope_on_no_day(
    tmp_path: Path,
) -> None:
    day = date(2026, 3, 5)
    _two_repository_store(tmp_path / "cache", b_entered=day, b_left=day)

    result = _invoke_selected(tmp_path, "--repo", "example/a")

    assert result.exit_code == 0, result.output
    assert len(result.output.split()) == 3


@pytest.mark.xfail(strict=True)
@pytest.mark.usefixtures("_isolated")
def test_render_command_reports_a_store_it_cannot_read(tmp_path: Path) -> None:
    from github_statistics.store import store_path

    path = store_path(tmp_path / "cache", "api.github.com", "example")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a database" * 100)

    result = _invoke_selected(tmp_path)

    assert result.exit_code == 1
    assert f"cannot read the store at {path}" in result.output
    assert isinstance(result.exception, SystemExit)
    assert not (tmp_path / "site").exists()


@pytest.mark.xfail(strict=True)
def test_render_size_must_keep_the_master_shape(tmp_path: Path) -> None:
    from github_statistics.config.models import Render

    with pytest.raises(ValueError, match="multiple of 16x9"):
        _render(tmp_path, _config(render=Render(width=1000, height=500)))
