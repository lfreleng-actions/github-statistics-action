# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The pages of the site and the assets they load (design §8.5).

`build_site` writes HTML, ``.nojekyll`` and ``assets/`` and nothing
else. It reads no store and makes no request: each target's series and
tables come in built, and the charts and data files are the ones
`render` and `export` wrote under ``<out>/<owner>/``, linked where they
exist and named as missing where they do not. Downloads link the JSON
documents `export` writes, by name; nothing else in ``data/`` is
linked. Logins and repository names reach the pages only through
autoescaped templates.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape
from PIL import Image

from github_statistics import __version__

from ..config import Config, ResolvedIdentities, Target, resolve_identities
from ..render.charts import VIEW_LABELS
from ..scope.resolver import is_exact
from ..series import Contributor, RepositoryRow, TargetSeries
from ..series.daily import SERIES
from ..series.leaderboard import leaderboard_columns
from ..series.repositories import COLUMNS as REPOSITORY_COLUMNS
from ..store import path_component
from .coverage import coverage_lines, coverage_note, days_text, figure

DATATABLES_VERSION = "9.0.3"
DATATABLES_CSS_URL = (
    "https://cdn.jsdelivr.net/npm/simple-datatables@9.0.3/dist/style.css"
)
DATATABLES_CSS_SRI = (
    "sha384-xnK68E/OAsSGcbvbeWEOyhjix2K7rBxt8Eytj/Ow9zuPG7WwFGGqMPQ8SbexlsL0"
)
DATATABLES_JS_URL = (
    "https://cdn.jsdelivr.net/npm/simple-datatables@9.0.3/dist/umd/"
    "simple-datatables.min.js"
)
DATATABLES_JS_SRI = (
    "sha384-JYQd44jQWQbU+FdjWIUlbjzENGRHPdOQcj7dAgjJEvSyt2js5lE85kaPOdC53JVu"
)
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net;"
    " style-src 'self' https://cdn.jsdelivr.net; img-src 'self' data:;"
    " base-uri 'none'; form-action 'none'"
)
TEMPLATES = (
    "base.html.j2",
    "macros.html.j2",
    "index.html.j2",
    "target.html.j2",
    "site.js.j2",
)
ASSETS = ("site.css",)
# The stems of the JSON documents `export` writes (the keys of
# `github_statistics.export.build_documents`), in the order linked.
DATA_DOCUMENTS = (
    "series",
    "contributors",
    "repositories",
    "coverage",
    "reviews",
    "meta",
)
PER_PAGE = 50
PER_PAGE_SELECT = (25, 50, 100, 250)
INLINE_WIDTH = 1920
HEADINGS = {
    "pull_requests": "Pull requests",
    "issues": "Issues",
    "workflows": "Workflow runs and jobs",
    "workflow_outcomes": "Workflow outcomes",
}
CLASS_LABELS = {
    "member": "member",
    "external": "external contributor",
    "org_bot": "our bot",
    "external_bot": "external bot",
}
BOT_CLASSES = ("org_bot", "external_bot")


@dataclass(frozen=True)
class SiteTarget:
    """What the pages of one target show, built from its store.

    ``merged`` counts the pull requests merged in the window under the
    daily series' rules, deleted authors included, which the leaderboard
    leaves out. ``identities`` are the lists the tables were classed
    with; ``None`` means the configuration's, as `resolve_identities`
    gives them. ``runs_horizon`` is the owner's runs horizon, ``None``
    when no run is held. ``job_runs`` is ``(counted, concluded)``: the
    concluded runs in the window and how many of them have a job count;
    ``None`` when not read. ``runs_partial`` is whether a workflows day
    lacks some repository's runs, unread job counts aside; ``None`` when
    not read.
    """

    target: Target
    series: TargetSeries
    contributors: Sequence[Contributor]
    repositories: Sequence[RepositoryRow]
    merged: int
    identities: ResolvedIdentities | None = None
    runs_horizon: date | None = None
    job_runs: tuple[int, int] | None = None
    runs_partial: bool | None = None


def _environment() -> Environment:
    """The template environment: autoescape on for HTML templates, off for the script."""
    return Environment(
        loader=PackageLoader("github_statistics", "site/templates"),
        autoescape=select_autoescape(
            enabled_extensions=("html.j2",), default_for_string=True, default=False
        ),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def _window(series: TargetSeries) -> str:
    """The window in words: its length and its first and last days."""
    days = series.days
    return (
        f"{days_text(len(days))}, {days[0].isoformat()} to {days[-1].isoformat()} (UTC)"
    )


def _scope_rule(target: Target) -> str:
    """The rule that selects ``target``'s repositories, in words.

    Where every ``include`` entry is an exact name the resolver reads
    those repositories alone and keeps each whatever its archived, fork
    or template state, so the rule says that rather than the toggles.
    """
    if target.include and all(map(is_exact, target.include)):
        return (
            f"Public repositories of {target.owner} named"
            f" {', '.join(target.include)}; a named repository counts"
            " whether or not it is archived, a fork or a template."
        )
    chosen = f"public repositories of {target.owner}"
    if target.include:
        chosen += f" matching {', '.join(target.include)}"
    if target.exclude:
        chosen += f", except {', '.join(target.exclude)}"
    if not (target.include or target.exclude):
        chosen = f"all {chosen}"
    kinds = (
        ("archived repositories", target.scan_archived),
        ("forks", target.scan_forks),
        ("templates", target.scan_templates),
    )
    flags = [f"{name} {'included' if on else 'left out'}" for name, on in kinds]
    return f"{chosen[0].upper()}{chosen[1:]}; {', '.join(flags)}."


def _total(series: TargetSeries, metric: str, key: str, view: str = "all") -> int:
    """The sum over the window of one series of one view."""
    return sum(series.values(metric, key, view))


def _headline(site: SiteTarget) -> list[tuple[str, str]]:
    """The landing card's numbers for the window, for the metrics charted."""
    series = site.series
    charted = set(series.coverage)
    numbers: list[tuple[str, str]] = []
    if "pull_requests" in charted:
        opened = _total(series, "pull_requests", "opened")
        numbers.append(("Pull requests opened", figure(site, "pull_requests", opened)))
        merged = figure(site, "pull_requests", site.merged)
        numbers.append(("Pull requests merged", merged))
    if "issues" in charted:
        for label, key in (("Issues opened", "opened"), ("Issues closed", "closed")):
            numbers.append(
                (label, figure(site, "issues", _total(series, "issues", key)))
            )
    if "workflows" in charted:
        runs = _total(series, "workflows", "runs")
        numbers.append(("Workflow runs", figure(site, "workflows", runs)))
        jobs = _total(series, "workflows", "jobs")
        numbers.append(("Jobs", figure(site, "workflows", jobs, jobs=True)))
    numbers.append(("Repositories in scope", str(series.repositories[-1])))
    return numbers


def _chart(
    owner_dir: Path, site: SiteTarget, metric: str, view: str
) -> dict[str, Any] | None:
    """The image of one chart, its master and its text; ``None`` when not rendered.

    The image is the `INLINE_WIDTH` derivative, or the master where
    `render` wrote no derivative at that width. Paths are relative to
    the target's directory.
    """
    stem = f"{metric}-{view}".replace("_", "-")
    master = owner_dir / "img" / f"{stem}.png"
    inline = owner_dir / "img" / f"{stem}-{INLINE_WIDTH}.png"
    shown = inline if inline.is_file() else master
    if not shown.is_file():
        return None
    with Image.open(shown) as image:
        width, height = image.size
    first, second = SERIES[metric]
    series = site.series
    figures = [
        figure(
            site,
            metric,
            _total(series, metric, key, view),
            key,
            jobs=(metric, key) == ("workflows", "jobs"),
            view=view,
        )
        for key in (first, second)
    ]
    alt = (
        f"{site.target.owner} — {HEADINGS[metric].lower()} per day,"
        f" {VIEW_LABELS[view]}, {_window(series)}: {', '.join(figures)}"
    )
    return {
        "src": f"img/{shown.name}",
        "href": f"img/{master.name}" if master.is_file() else f"img/{shown.name}",
        "width": width,
        "height": height,
        "alt": alt,
    }


def _lazy_after_first(charts: Iterable[dict[str, Any] | None]) -> None:
    """Mark every chart of a page but the first to load lazily."""
    present = [chart for chart in charts if chart is not None]
    for index, chart in enumerate(present):
        chart["lazy"] = index > 0


def _target_page(site: SiteTarget, config: Config, owner_dir: Path) -> dict[str, Any]:
    """The context of one target's page."""
    metrics: list[dict[str, Any]] = []
    for metric, grades in site.series.coverage.items():
        views = [
            {
                "id": f"{metric}-{view}".replace("_", "-"),
                "label": VIEW_LABELS[view],
                "chart": _chart(owner_dir, site, metric, view),
            }
            for view in config.classes
        ]
        metrics.append(
            {
                "name": metric.replace("_", "-"),
                "heading": HEADINGS[metric],
                "views": views,
                "notes": coverage_note(metric, grades),
            }
        )
    _lazy_after_first(view["chart"] for m in metrics for view in m["views"])
    columns = leaderboard_columns(config)
    identities = site.identities or resolve_identities(site.target, config)
    data_dir = owner_dir / "data"
    downloads = [
        f"{stem}.json"
        for stem in DATA_DOCUMENTS
        if (data_dir / f"{stem}.json").is_file()
    ]
    return {
        "owner": site.target.owner,
        "member_orgs": identities.member_orgs.values,
        "named_members": bool(identities.members.values),
        "metrics": metrics,
        "columns": columns,
        "contributors": [
            {"login": row.login, "class": row.actor_class.value, "counts": row.counts}
            for row in site.contributors
        ],
        "repository_columns": REPOSITORY_COLUMNS,
        "coverage_metrics": list(site.series.coverage),
        "repositories": site.repositories,
        "downloads": downloads,
        "window": _window(site.series),
        "scope_rule": _scope_rule(site.target),
        "coverage_lines": coverage_lines(site),
    }


def _card(
    site: SiteTarget, config: Config, owner_dir: Path, directory: str
) -> dict[str, Any]:
    """The context of one target's card on the landing page.

    ``charts`` holds an entry per charted metric, its chart ``None``
    where `render` wrote none, so the card names each missing chart.
    The previews are the ``all`` view: when ``classes`` leaves it out,
    `render` writes none, so ``charts`` is empty and ``previews`` false.
    """
    previews = "all" in config.classes
    charts = []
    for metric in site.series.coverage if previews else ():
        chart = _chart(owner_dir, site, metric, "all")
        if chart is not None:
            chart = {
                **chart,
                "src": f"{directory}/{chart['src']}",
                "href": f"{directory}/{chart['href']}",
            }
        charts.append({"heading": HEADINGS[metric], "chart": chart})
    return {
        "owner": site.target.owner,
        "href": f"{directory}/index.html",
        "headline": _headline(site),
        "coverage_lines": coverage_lines(site),
        "charts": charts,
        "previews": previews,
        "scope_rule": _scope_rule(site.target),
    }


def _write(path: Path, text: str, written: list[Path]) -> None:
    """Write ``text`` to ``path`` as UTF-8 and record the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    written.append(path)


def build_site(
    targets: Sequence[SiteTarget], config: Config, out_dir: Path, *, now: datetime
) -> list[Path]:
    """Write the site for ``targets`` under ``out_dir``; the files written, in order.

    ``now`` gives the generation date shown in each footer, the date
    alone, so two builds on one day write identical pages. ``out_dir``
    must hold the charts and data of the same run: this function links
    what `render` and `export` wrote and does not check their freshness.
    """
    out = Path(out_dir)
    environment = _environment()
    common = {
        "csp": CONTENT_SECURITY_POLICY,
        "version": __version__,
        "generated": now.date().isoformat(),
        "class_labels": CLASS_LABELS,
        "bot_classes": BOT_CLASSES,
    }
    written: list[Path] = []
    _write(out / ".nojekyll", "", written)
    assets = files("github_statistics.site") / "assets"
    for name in ASSETS:
        _write(out / "assets" / name, (assets / name).read_text("utf-8"), written)
    script = environment.get_template("site.js.j2").render(
        per_page=PER_PAGE, per_page_select=list(PER_PAGE_SELECT)
    )
    _write(out / "assets" / "site.js", script, written)
    target_page = environment.get_template("target.html.j2")
    cards = []
    for site in targets:
        directory = path_component(site.target.owner, "owner")
        owner_dir = out / directory
        context = _target_page(site, config, owner_dir)
        page = target_page.render(
            **common,
            **context,
            root="../",
            datatables_css_url=DATATABLES_CSS_URL,
            datatables_css_sri=DATATABLES_CSS_SRI,
            datatables_js_url=DATATABLES_JS_URL,
            datatables_js_sri=DATATABLES_JS_SRI,
        )
        _write(owner_dir / "index.html", page, written)
        cards.append(_card(site, config, owner_dir, directory))
    _lazy_after_first(entry["chart"] for card in cards for entry in card["charts"])
    window = _window(targets[0].series) if targets else ""
    landing = environment.get_template("index.html.j2").render(
        **common, root="", cards=cards, window=window
    )
    _write(out / "index.html", landing, written)
    return written
