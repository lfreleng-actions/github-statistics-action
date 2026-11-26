# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the ``run`` command and the GitHub output writer (design §4.2, §9.1)."""

from __future__ import annotations

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


def _result(owner: str, **changes: Any) -> Any:
    """A `CollectResult` for ``owner``, with ``changes`` applied."""
    from github_statistics.collect import CollectResult, MetricCoverage

    values: dict[str, Any] = {
        "owner": owner,
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


def _stages(
    monkeypatch: pytest.MonkeyPatch,
    outcome: Any = None,
    bots: tuple[str, ...] = (),
) -> list[tuple[str, str]]:
    """Stub every stage of ``run``; the calls in order, as (stage, owner) pairs.

    Collection returns or raises ``outcome``, by default a clean result
    for the target's owner; each target reports ``bots`` as unclassified.
    """
    import github_statistics.cli.run as run_module

    import github_statistics.cli.collect as collect_module

    calls: list[tuple[str, str]] = []

    async def collect(_config: Any, target: Any, **_kwargs: Any) -> Any:
        calls.append(("collect", target.owner))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome or _result(target.owner)

    def stage(name: str) -> Any:
        def record(_resolution: Any, targets: Any, **_kwargs: Any) -> list[Any]:
            calls.extend((name, target.owner) for target in targets)
            return [f"{name}-{target.owner}" for target in targets]

        return record

    def write_site(sites: Any, _config: Any, **_kwargs: Any) -> list[Path]:
        calls.append(
            ("write_site", ",".join(site.rpartition("-")[2] for site in sites))
        )
        return []

    monkeypatch.setattr(collect_module, "collect", collect)
    monkeypatch.setattr(run_module, "render_targets", stage("render"))
    monkeypatch.setattr(run_module, "export_targets", stage("export"))
    monkeypatch.setattr(run_module, "site_targets", stage("site"))
    monkeypatch.setattr(run_module, "write_site", write_site)
    monkeypatch.setattr(run_module, "unclassified", lambda *_args: list(bots))
    return calls


def _invoke(tmp_path: Path, *args: str) -> Any:
    """Run ``run`` with the cache and site under ``tmp_path`` and the rate limit mocked."""
    from github_statistics.cli import app

    with respx.mock:
        respx.get(f"{API}/rate_limit").respond(404, json={})
        return runner.invoke(
            app,
            [
                "run",
                "--cache-dir",
                str(tmp_path / "cache"),
                "--output-dir",
                str(tmp_path / "site"),
                *args,
            ],
        )


def _heredocs(text: str) -> list[tuple[str, str]]:
    """The ``(key, value)`` pairs of ``text``, each in the multiline form.

    Fails unless every output is ``key<<DELIMITER``, its value and the
    delimiter alone on the closing line, with a delimiter its value lacks.
    """
    lines = text.splitlines()
    assert len(lines) % 3 == 0, lines
    pairs = []
    for start in range(0, len(lines), 3):
        key, _, delimiter = lines[start].partition("<<")
        assert delimiter, lines[start]
        assert lines[start + 2] == delimiter
        assert delimiter not in lines[start + 1]
        pairs.append((key, lines[start + 1]))
    return pairs


@pytest.mark.xfail(strict=True, reason="run is not implemented yet")
def test_stages_run_in_order_per_target_and_the_site_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each target is collected, rendered, exported and read for the site in turn."""
    calls = _stages(monkeypatch)

    result = _invoke(tmp_path, "--repo", "acme/a", "--repo", "beta/b")

    assert result.exit_code == 0, result.output
    assert calls == [
        ("collect", "acme"),
        ("render", "acme"),
        ("export", "acme"),
        ("site", "acme"),
        ("collect", "beta"),
        ("render", "beta"),
        ("export", "beta"),
        ("site", "beta"),
        ("write_site", "acme,beta"),
    ]


@pytest.mark.xfail(strict=True, reason="run is not implemented yet")
def test_a_spent_budget_still_renders_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A spent budget leaves gaps in the charts but not a failed run."""
    calls = _stages(monkeypatch, _result("acme", exhausted="backfill"))

    result = _invoke(tmp_path, "--repo", "acme/a")

    assert result.exit_code == 0, result.output
    assert "backfill" in result.output
    assert [stage for stage, _owner in calls] == [
        "collect",
        "render",
        "export",
        "site",
        "write_site",
    ]


@pytest.mark.xfail(strict=True, reason="run is not implemented yet")
def test_a_rejected_token_exits_three_before_rendering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 401 stops the run: nothing is rendered and the token stays secret."""
    from github_statistics.client import AuthError

    monkeypatch.setenv("GHS_TOKEN", "ghp_secretvalue")
    calls = _stages(monkeypatch, AuthError("401 Unauthorized"))

    result = _invoke(tmp_path, "--repo", "acme/a", "--token-env", "GHS_TOKEN")

    assert result.exit_code == 3
    assert "$GHS_TOKEN" in result.output
    assert "ghp_secretvalue" not in result.output
    assert calls == [("collect", "acme")]


@pytest.mark.xfail(strict=True, reason="run is not implemented yet")
def test_an_invalid_configuration_exits_one_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no configuration source at all, no stage runs."""
    calls = _stages(monkeypatch)

    result = _invoke(tmp_path)

    assert result.exit_code == 1
    assert calls == []


@pytest.mark.xfail(strict=True, reason="run is not implemented yet")
def test_strict_exits_two_after_building_the_site(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--strict`` fails an unreadable repository once the site is written."""
    calls = _stages(monkeypatch, _result("acme", unreadable={"acme/a": "HTTP 502"}))

    lenient = _invoke(tmp_path, "--repo", "acme/a")
    strict = _invoke(tmp_path, "--repo", "acme/a", "--strict")

    assert lenient.exit_code == 0, lenient.output
    assert strict.exit_code == 2, strict.output
    assert calls[-1] == ("write_site", "acme")


@pytest.mark.xfail(strict=True, reason="run is not implemented yet")
def test_writes_each_output_once_and_a_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The outputs reach ``$GITHUB_OUTPUT`` once and a summary the step summary."""
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    _stages(monkeypatch, bots=("helper[bot]", "zeta-bot"))

    result = _invoke(tmp_path, "--repo", "acme/a", "--repo", "beta/b")

    assert result.exit_code == 0, result.output
    assert _heredocs(output.read_text(encoding="utf-8")) == [
        ("site_dir", str(tmp_path / "site")),
        ("requests_used", "14"),
        ("coverage_complete", "false"),
        ("gap_days", "2"),
        ("unclassified_bots", "helper[bot],zeta-bot"),
    ]
    text = summary.read_text(encoding="utf-8")
    assert "### Collection" in text
    assert "helper[bot]" in text


@pytest.mark.xfail(strict=True, reason="run is not implemented yet")
def test_warns_about_unclassified_bots_naming_the_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every run ends naming each unclassified bot and where to classify it."""
    _stages(monkeypatch, bots=("helper[bot]",))

    result = _invoke(tmp_path, "--repo", "acme/a")

    assert result.exit_code == 0, result.output
    assert "helper[bot]" in result.output
    assert "identities.org_bots or identities.external_bots" in result.output


@pytest.mark.xfail(strict=True, reason="run is not implemented yet")
def test_refuses_an_output_value_that_injects_another(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A site directory carrying a newline never reaches ``$GITHUB_OUTPUT``."""
    from github_statistics.cli import app

    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    _stages(monkeypatch)

    with respx.mock:
        respx.get(f"{API}/rate_limit").respond(404, json={})
        result = runner.invoke(
            app,
            [
                "run",
                "--repo",
                "acme/a",
                "--cache-dir",
                str(tmp_path / "cache"),
                "--output-dir",
                str(tmp_path / "site\nrequests_used=0"),
            ],
        )

    assert result.exit_code == 1
    assert not output.exists() or output.read_text(encoding="utf-8") == ""


@pytest.mark.xfail(strict=True, reason="run is not implemented yet")
def test_clears_each_targets_charts_and_data_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Stale charts and data go before a target is written; nothing else does."""
    _stages(monkeypatch)
    site = tmp_path / "site"
    stale = [site / "acme" / "img" / "old.png", site / "acme" / "data" / "old.json"]
    kept = [site / "acme" / "notes.txt", site / "other" / "img" / "x.png"]
    for path in (*stale, *kept):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    result = _invoke(tmp_path, "--repo", "acme/a")

    assert result.exit_code == 0, result.output
    assert not any(path.exists() for path in stale)
    assert all(path.exists() for path in kept)


@pytest.mark.xfail(strict=True, reason="the shared output writer does not exist yet")
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("site_dir", "a\nb=c"),
        ("site_dir", "a\rb"),
        ("site_dir", "x<<EOF"),
        ("bad key", "x"),
        ("a=b", "x"),
        ("", "x"),
    ],
)
def test_write_outputs_refuses_injection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str, value: str
) -> None:
    """A key or value able to start another output writes nothing and fails."""
    import typer
    from github_statistics.cli.common import write_outputs

    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    with pytest.raises(typer.Exit):
        write_outputs({"gap_days": "1", key: value})

    assert not output.exists()


@pytest.mark.xfail(strict=True, reason="the shared output writer does not exist yet")
def test_write_outputs_appends_plain_pairs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Safe pairs append one ``key=value`` line each, in order."""
    from github_statistics.cli.common import write_outputs

    output = tmp_path / "output"
    output.write_text("earlier=1\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    write_outputs({"gap_days": "3", "unclassified_bots": "a[bot],b"})

    assert output.read_text(encoding="utf-8").splitlines() == [
        "earlier=1",
        "gap_days=3",
        "unclassified_bots=a[bot],b",
    ]


@pytest.mark.xfail(strict=True, reason="the multiline form is not written yet")
def test_write_outputs_heredocs_with_a_delimiter_no_value_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The multiline form picks another delimiter when a value equals one."""
    import uuid

    import github_statistics.cli.common as common

    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    tokens = iter([uuid.UUID(int=1), uuid.UUID(int=1), uuid.UUID(int=2)])
    monkeypatch.setattr(common.uuid, "uuid4", lambda: next(tokens))
    taken = f"ghadelimiter_{uuid.UUID(int=1)}"

    common.write_outputs({"gap_days": "3", "site_dir": taken}, heredoc=True)

    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines[0] == f"gap_days<<{taken}"
    assert lines[3] == f"site_dir<<ghadelimiter_{uuid.UUID(int=2)}"
    assert _heredocs("\n".join(lines)) == [("gap_days", "3"), ("site_dir", taken)]
