# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Workflow runs as the Actions listing returns them (design §6.4)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from github_statistics.store import Run


def listed_run(repo: str, data: dict[str, Any]) -> Run:
    """The stored record of one listed run.

    The actor is ``triggering_actor``, falling back to ``actor``; what a
    scheduled run's actor means is for classification (design §7.1).
    """
    actor = data.get("triggering_actor") or data.get("actor")
    return Run(
        id=int(data["id"]),
        node_id=data["node_id"],
        repo=repo,
        workflow_path=data["path"],
        event=data["event"],
        status=data["status"],
        conclusion=data.get("conclusion"),
        created_at=datetime.fromisoformat(data["created_at"]),
        actor=None if actor is None else actor["login"],
        actor_type=None if actor is None else actor.get("type"),
        attempt=int(data.get("run_attempt", 1)),
    )


def reaches(runs: Iterable[Run], since: datetime) -> bool:
    """Whether any of ``runs`` was created before ``since``.

    Strictly before: runs sharing the second of ``since`` may run on
    past the page that holds the first of them.
    """
    return any(run.created_at < since for run in runs)
