# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the composite action's interface (design §9.1); no shell runs."""

from __future__ import annotations

import re
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
_INPUT_REFERENCE = re.compile(r"\binputs\.([A-Za-z0-9_-]+)")


def _action() -> dict[str, Any]:
    """The parsed ``action.yaml``."""
    with ACTION.open(encoding="utf-8") as handle:
        document: dict[str, Any] = yaml.safe_load(handle)
    return document


def _steps() -> list[dict[str, Any]]:
    """The composite action's steps."""
    return list(_action()["runs"]["steps"])


@pytest.mark.xfail(strict=True, reason="the composite action is not written yet")
def test_inputs_are_optional_and_default_to_the_empty_string() -> None:
    """A non-empty default would reach whatever release the pin installs."""
    inputs = _action()["inputs"]

    assert set(inputs) == INPUTS
    for name, spec in inputs.items():
        assert spec.get("default") == "", name
        assert not spec.get("required", False), name


@pytest.mark.xfail(strict=True, reason="the composite action is not written yet")
def test_outputs_are_declared() -> None:
    """The run and scope outputs are all published."""
    assert set(_action()["outputs"]) == OUTPUTS


@pytest.mark.xfail(strict=True, reason="the composite action is not written yet")
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


@pytest.mark.xfail(strict=True, reason="the composite action is not written yet")
def test_remote_actions_are_pinned_to_a_commit() -> None:
    """Each remote ``uses:`` names a full commit SHA."""
    uses = [step["uses"] for step in _steps() if "uses" in step]

    assert uses
    for reference in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", reference), reference
