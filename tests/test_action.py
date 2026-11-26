# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the composite action's interface (design §9.1); no shell runs."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ACTION = Path(__file__).resolve().parent.parent / "action.yaml"

INPUTS = {
    "command",
    "config",
    "org",
    "repositories",
    "exclude",
    "repo",
    "token",
    "days",
    "until",
    "output_dir",
    "cache_dir",
    "budget",
    "refresh",
    "strict",
    "org_bots",
    "external_bots",
    "members",
    "member_orgs",
    "ignore_logins",
    "tool_version",
    "use_local_source",
}
OUTPUTS = {
    "site_dir",
    "coverage_complete",
    "gap_days",
    "requests_used",
    "unclassified_bots",
    "repositories",
    "repository_count",
}
TOKEN = "ghs_secret_value"
_INPUT_REFERENCE = re.compile(r"\binputs\.([A-Za-z0-9_-]+)")


def _action() -> dict[str, Any]:
    """The parsed ``action.yaml``."""
    with ACTION.open(encoding="utf-8") as handle:
        document: dict[str, Any] = yaml.safe_load(handle)
    return document


def _steps() -> list[dict[str, Any]]:
    """The composite action's steps."""
    return list(_action()["runs"]["steps"])


def test_inputs_are_optional_and_default_to_the_empty_string() -> None:
    """A non-empty default would reach whatever release the pin installs."""
    inputs = _action()["inputs"]

    assert set(inputs) == INPUTS
    for name, spec in inputs.items():
        assert spec.get("default") == "", name
        assert not spec.get("required", False), name


def test_outputs_are_declared() -> None:
    """The run and scope outputs are all published."""
    assert set(_action()["outputs"]) == OUTPUTS


def test_every_input_reaches_the_script_through_env_alone() -> None:
    """Inputs reach the shell as environment variables, never as script text."""
    steps = _steps()
    scripts = [step for step in steps if "run" in step]
    assert len(scripts) == 1
    [script] = scripts

    for step in steps:
        assert "${{" not in step.get("run", ""), step.get("name")
    referenced = {
        name
        for value in script.get("env", {}).values()
        for name in _INPUT_REFERENCE.findall(str(value))
    }
    assert referenced == INPUTS


def test_remote_actions_are_pinned_to_a_commit() -> None:
    """Each remote ``uses:`` names a full commit SHA."""
    uses = [step["uses"] for step in _steps() if "uses" in step]

    assert uses
    for reference in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", reference), reference


def _script_step() -> dict[str, Any]:
    """The action's one ``run`` step."""
    [script] = [step for step in _steps() if "run" in step]
    return script


def _run_script(tmp_path: Path, **inputs: str) -> subprocess.CompletedProcess[str]:
    """Run the step's script under bash with a stub ``uvx`` on ``PATH``.

    The stub records its arguments and ``GITHUB_TOKEN``, so a test can see
    what reached the tool.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uvx = bin_dir / "uvx"
    uvx.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$@" > "$STUB_DIR/args"\n'
        'printenv GITHUB_TOKEN > "$STUB_DIR/token" || true\n',
        encoding="utf-8",
    )
    uvx.chmod(0o755)
    env = {
        **dict.fromkeys(_script_step()["env"], ""),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GITHUB_ACTION_PATH": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "STUB_DIR": str(tmp_path),
        "GITHUB_TOKEN": TOKEN,
        "GHSA_EVENT_NAME": "pull_request",
        "GHSA_OUTPUT_DIR": "site",
        **inputs,
    }
    return subprocess.run(  # noqa: S603
        ["bash", "-c", _script_step()["run"]],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_token_reaches_the_tool_as_github_token(tmp_path: Path) -> None:
    """The tool sees the token as GITHUB_TOKEN, never in its arguments."""
    assert _script_step()["env"]["GITHUB_TOKEN"] == (
        "${{ inputs.token || github.token }}"
    )

    result = _run_script(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "token").read_text(encoding="utf-8").strip() == TOKEN
    args = (tmp_path / "args").read_text(encoding="utf-8").splitlines()
    assert TOKEN not in args
    assert "--token-env" not in args
    assert args[args.index("--output-dir") + 1] == "site"
    assert TOKEN not in result.stdout + result.stderr


def test_run_passes_its_switches(tmp_path: Path) -> None:
    """``refresh`` and ``strict`` reach ``run`` as flags."""
    result = _run_script(tmp_path, GHSA_REFRESH="true", GHSA_STRICT="true")

    assert result.returncode == 0, result.stdout + result.stderr
    args = (tmp_path / "args").read_text(encoding="utf-8").splitlines()
    assert {"run", "--refresh", "--strict"} <= set(args)


@pytest.mark.parametrize("switch", ["refresh", "strict"])
def test_scope_refuses_a_run_only_switch(tmp_path: Path, switch: str) -> None:
    """``scope`` takes neither switch, so setting one fails before the tool runs."""
    result = _run_script(
        tmp_path,
        GHSA_COMMAND="scope",
        **{f"GHSA_{switch.upper()}": "true"},
    )

    assert result.returncode == 1
    assert f"{switch} applies to command 'run' only" in result.stdout
    assert not (tmp_path / "args").exists()


def test_scope_leaves_out_run_only_switches(tmp_path: Path) -> None:
    """``scope`` with both switches off passes neither flag."""
    result = _run_script(
        tmp_path,
        GHSA_COMMAND="scope",
        GHSA_REFRESH="false",
        GHSA_STRICT="false",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    args = (tmp_path / "args").read_text(encoding="utf-8").splitlines()
    assert "scope" in args
    assert "--refresh" not in args
    assert "--strict" not in args
