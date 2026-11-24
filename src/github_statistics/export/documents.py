# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The documents a target publishes under ``data/`` (design §8.6).

Each document is self-contained plain data built from the store and
the configuration alone: the window end and the generation date are
passed in, and nothing here reads a clock or makes a request.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..collect import runs_horizon
from ..config import Config, ResolvedIdentities, Target, resolve_identities
from ..config.loader import dump
from ..render.model import Coverage
from ..series import (
    TargetSeries,
    build_leaderboard,
    build_repositories,
    build_series,
    classify,
)
from ..series.actors import normalise, public_members
from ..series.leaderboard import BOT_TYPE, display_login, leaderboard_columns
from ..series.repositories import COLUMNS as REPOSITORY_COLUMNS
from ..store import Store
from ..store.codec import timestamp

# The name each coverage grade takes in ``coverage.json``.
COVERAGE_NAMES = {
    Coverage.COMPLETE: "complete",
    Coverage.PARTIAL: "partial",
    Coverage.NONE: "missing",
}
Documents = dict[str, dict[str, Any]]


def _series(owner: str, built: TargetSeries) -> dict[str, Any]:
    """``series.json``: per metric, series and class, one ``{day, value}`` per day."""
    days = [day.isoformat() for day in built.days]
    metrics: dict[str, Any] = {
        metric: {
            "coverage": [
                {"day": day, "coverage": grade.value}
                for day, grade in zip(days, grades, strict=True)
            ],
            "series": {},
        }
        for metric, grades in built.coverage.items()
    }
    for (metric, series, actor_class), values in built.counts.items():
        classes = metrics[metric]["series"].setdefault(series, {})
        classes[actor_class] = [
            {"day": day, "value": value}
            for day, value in zip(days, values, strict=True)
        ]
    return {"owner": owner, "metrics": metrics}


def _coverage(owner: str, built: TargetSeries, horizon: date | None) -> dict[str, Any]:
    """``coverage.json``: per metric, the days complete, partial and missing."""
    metrics = {}
    for metric, grades in built.coverage.items():
        named: dict[str, list[str]] = {name: [] for name in COVERAGE_NAMES.values()}
        for day, grade in zip(built.days, grades, strict=True):
            named[COVERAGE_NAMES[grade]].append(day.isoformat())
        metrics[metric] = named
    return {
        "owner": owner,
        "runs_horizon": horizon.isoformat() if horizon else None,
        "metrics": metrics,
    }


def _reviews(
    store: Store,
    target: Target,
    identities: ResolvedIdentities,
    days: tuple[date, ...],
) -> dict[str, Any]:
    """``reviews.json``: every review in the window, its reviewer and class.

    Reviews of repositories out of scope on the day, and of ignored
    logins, are left out, as everywhere else.
    """
    scope = {day: set(store.in_scope(day)) for day in days}
    members = public_members(store, identities, owner=target.owner)
    reviews = store.reviews_between(days[0], days[-1])
    forms: dict[str, set[str]] = {}
    bots: set[str] = set()
    for review in reviews:
        if review.reviewer is not None:
            key = normalise(review.reviewer)
            forms.setdefault(key, set()).add(review.reviewer)
            if review.reviewer_type == BOT_TYPE:
                bots.add(key)
    shown = {
        key: display_login(found, identities, bot=key in bots)
        for key, found in forms.items()
    }
    records = []
    for review in reviews:
        if review.submitted_at is None:
            continue
        if review.repo not in scope.get(review.submitted_at.date(), set()):
            continue
        actor_class = classify(
            review.reviewer, review.reviewer_type, identities, members
        )
        if actor_class is None:
            continue
        reviewer = review.reviewer
        records.append(
            {
                "repository": review.repo,
                "number": review.number,
                "reviewer": shown[normalise(reviewer)] if reviewer else None,
                "class": actor_class.value,
                "state": review.state,
                "submitted_at": timestamp(review.submitted_at),
            }
        )
    return {"owner": target.owner, "reviews": records}


def build_documents(
    store: Store,
    config: Config,
    target: Target,
    *,
    until: date,
    generated: date,
    version: str,
    identities: ResolvedIdentities | None = None,
) -> Documents:
    """Every document of ``target``, keyed by file stem.

    ``until`` ends the window, ``generated`` is the date recorded in
    ``meta.json`` and ``version`` the tool version recorded beside it.
    """
    if identities is None:
        identities = resolve_identities(target, config)
    owner = target.owner
    built = build_series(store, config, target, until=until, identities=identities)
    contributors = build_leaderboard(
        store, config, target, until=until, identities=identities
    )
    repositories = build_repositories(
        store, config, target, until=until, identities=identities
    )
    horizon = runs_horizon(store, owner)
    return {
        "series": _series(owner, built),
        "contributors": {
            "owner": owner,
            "columns": list(leaderboard_columns(config)),
            "contributors": [row.record() for row in contributors],
        },
        "repositories": {
            "owner": owner,
            "columns": list(REPOSITORY_COLUMNS),
            "metrics": list(built.coverage),
            "repositories": [row.record() for row in repositories],
        },
        "coverage": _coverage(owner, built, horizon),
        "reviews": _reviews(store, target, identities, built.days),
        "meta": {
            "tool_version": version,
            "target": owner,
            "window": {
                "days": len(built.days),
                "from": built.days[0].isoformat(),
                "until": built.days[-1].isoformat(),
            },
            "scope_rule": {
                "include": list(target.include),
                "exclude": list(target.exclude),
                "scan_archived": target.scan_archived,
                "scan_forks": target.scan_forks,
                "scan_templates": target.scan_templates,
            },
            "generated": generated.isoformat(),
            "unclassified_bots": list(built.unclassified_bots),
            "identities": dump(identities),
        },
    }
