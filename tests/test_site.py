# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the static site and the ``site`` command (design §8.5)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import respx
from typer.testing import CliRunner

runner = CliRunner()

UNTIL = date(2026, 3, 10)
NOW = datetime(2026, 3, 11, 6, 30, tzinfo=UTC)
HOSTILE_LOGIN = "<img src=x onerror=1>"
HOSTILE_REPO = 'acme/"><script>alert(1)</script>'
METRICS = ("pull-requests", "issues", "workflows")
VIEWS = ("all", "member", "external", "org-bot", "external-bot")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's own configuration and cache out of every test."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("GHS_CONFIG", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


class _Page(HTMLParser):
    """Every start tag of a page, with its attributes, and each script's text."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.scripts: list[str] = []
        self._in_script = False
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self._in_script = True
            self.scripts.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self.scripts[-1] += data

    def find(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for name, attrs in self.tags if name == tag]


def _at(day: int) -> datetime:
    """Noon UTC on ``day`` March 2026."""
    return datetime(2026, 3, day, 12, tzinfo=UTC)


def _seed(cache: Path, owner: str, repo: str, author: str) -> None:
    """A store for ``owner``: ``repo`` in scope, one pull request, a bot review."""
    from github_statistics.collect import PULL_REQUESTS_METRIC
    from github_statistics.store import Item, Review, Store, store_path

    day = date(2026, 3, 1)
    with Store(store_path(cache, "api.github.com", owner)) as store:
        store.enter_scope(repo, day)
        store.replace_members(owner, ["alice"], day)
        store.add_coverage(repo, PULL_REQUESTS_METRIC, day, date(2026, 3, 5))
        store.upsert_items(
            [
                Item(
                    repo=repo,
                    number=1,
                    node_id=f"PR_{owner}",
                    kind="pr",
                    created_at=_at(2),
                    closed_at=_at(3),
                    merged_at=_at(3),
                    author=author,
                    author_type="User",
                    updated_at=_at(3),
                )
            ]
        )
        store.replace_reviews(
            repo,
            1,
            [
                Review(
                    1, f"PRR_{owner}", repo, 1, "dependabot", "Bot", "APPROVED", _at(3)
                )
            ],
        )


def _charts(out: Path, owner: str) -> None:
    """Small stand-ins for the charts `render` writes for ``owner``."""
    from PIL import Image

    img = out / owner / "img"
    img.mkdir(parents=True, exist_ok=True)
    for metric in METRICS:
        for view in VIEWS:
            Image.new("RGB", (32, 18)).save(img / f"{metric}-{view}.png")
            Image.new("RGB", (16, 9)).save(img / f"{metric}-{view}-1920.png")


def _run(tmp_path: Path, command: str, *orgs: str) -> Any:
    """Run ``command`` over ``orgs`` for the ten days to `UNTIL`."""
    from github_statistics.cli import app

    args = [command]
    for org in orgs:
        args += ["--org", org]
    args += [
        "--days",
        "10",
        "--until",
        UNTIL.isoformat(),
        "--cache-dir",
        str(tmp_path / "cache"),
        "--output-dir",
        str(tmp_path / "site"),
    ]
    return runner.invoke(app, args)


def _site(tmp_path: Path, *orgs: str, charts: bool = True, export: bool = True) -> Path:
    """Seed a store per owner, then optionally charts and data, then the site."""
    for org in orgs:
        _seed(tmp_path / "cache", org, f"{org}/a", "alice")
        if charts:
            _charts(tmp_path / "site", org)
    if export:
        assert _run(tmp_path, "export", *orgs).exit_code == 0
    with respx.mock(assert_all_called=False) as mock:
        result = _run(tmp_path, "site", *orgs)
    assert result.exit_code == 0, result.output
    assert not mock.calls
    return tmp_path / "site"


def _pages(site: Path) -> dict[Path, str]:
    """Every HTML page of ``site``, by path."""
    return {path: path.read_text(encoding="utf-8") for path in site.rglob("*.html")}


def test_site_writes_landing_target_pages_and_assets(tmp_path: Path) -> None:
    site = _site(tmp_path, "acme", "other")
    for name in (".nojekyll", "index.html", "assets/site.css", "assets/site.js"):
        assert (site / name).is_file(), name
    for owner in ("acme", "other"):
        page = (site / owner / "index.html").read_text(encoding="utf-8")
        assert "simple-datatables" in page
        hrefs = [a["href"] for a in _Page(page).find("a")]
        assert "data/contributors.json" in hrefs
        assert "img/pull-requests-member.png" in hrefs
    landing = _Page((site / "index.html").read_text(encoding="utf-8"))
    hrefs = [a["href"] for a in landing.find("a")]
    assert "acme/index.html" in hrefs
    assert "other/index.html" in hrefs
    sources = [img["src"] for img in landing.find("img")]
    assert "acme/img/pull-requests-all-1920.png" in sources
    assert "acme/img/workflows-all-1920.png" in sources


def test_relative_links_resolve(tmp_path: Path) -> None:
    site = _site(tmp_path, "acme", "other")
    checked = 0
    for path, text in _pages(site).items():
        page = _Page(text)
        for tag, key in (("a", "href"), ("img", "src"), ("link", "href")):
            for attrs in page.find(tag):
                url = attrs[key] or ""
                if urlsplit(url).scheme:
                    continue
                if url.startswith("#"):
                    assert f'id="{url[1:]}"' in text, (path, url)
                    continue
                assert (path.parent / url).resolve().is_file(), (path, url)
                checked += 1
        for attrs in page.find("script"):
            url = attrs["src"] or ""
            if not urlsplit(url).scheme:
                assert (path.parent / url).resolve().is_file(), (path, url)
    assert checked > 40


def test_external_assets_are_pinned_with_integrity(tmp_path: Path) -> None:
    site = _site(tmp_path, "acme")
    external = 0
    for text in _pages(site).values():
        assert "@latest" not in text
        page = _Page(text)
        tags = [("script", a, a.get("src")) for a in page.find("script")]
        tags += [("link", a, a.get("href")) for a in page.find("link")]
        for _, attrs, url in tags:
            if url and urlsplit(url).scheme:
                external += 1
                assert url.startswith("https://cdn.jsdelivr.net/npm/simple-datatables@")
                assert (attrs.get("integrity") or "").startswith("sha384-")
                assert attrs.get("crossorigin") == "anonymous"
    assert external == 2


def test_pages_carry_no_inline_script_or_style(tmp_path: Path) -> None:
    site = _site(tmp_path, "acme")
    for path, text in _pages(site).items():
        page = _Page(text)
        assert "default-src 'self'" in (
            next(
                m["content"] or ""
                for m in page.find("meta")
                if m.get("http-equiv") == "Content-Security-Policy"
            )
        ), path
        assert not page.find("style"), path
        assert all("style" not in attrs for _, attrs in page.tags), path
        assert all(attrs.get("src") for attrs in page.find("script")), path
        assert all(not body.strip() for body in page.scripts), path
        assert all(not k.startswith("on") for _, a in page.tags for k in a), path


def test_hostile_login_and_repository_are_escaped(tmp_path: Path) -> None:
    _seed(tmp_path / "cache", "acme", HOSTILE_REPO, HOSTILE_LOGIN)
    result = _run(tmp_path, "site", "acme")
    assert result.exit_code == 0, result.output
    text = (tmp_path / "site" / "acme" / "index.html").read_text(encoding="utf-8")
    assert HOSTILE_LOGIN not in text
    assert "<script>alert(1)" not in text
    assert "&lt;img src=x onerror=1&gt;" in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert not any(tag == "img" for tag, _ in _Page(text).tags)


def test_target_page_tables_tabs_and_notes(tmp_path: Path) -> None:
    site = _site(tmp_path, "acme")
    text = (site / "acme" / "index.html").read_text(encoding="utf-8")
    page = _Page(text)
    assert "Members are people whose membership of acme is public on GitHub" in text
    assert "configuration names as members" not in text
    rows = [a for a in page.find("tr") if "data-class" in a]
    assert {a["data-class"] for a in rows} == {"member", "external_bot"}
    radios = [a for a in page.find("input") if a.get("type") == "radio"]
    assert len(radios) == len(METRICS) * len(VIEWS)
    assert sum("checked" in a for a in radios) == len(METRICS)
    images = page.find("img")
    assert len(images) == len(METRICS) * len(VIEWS)
    assert "loading" not in images[0]
    assert all(img.get("loading") == "lazy" for img in images[1:])
    assert all(img["width"] == "16" and img["height"] == "9" for img in images)
    assert "acme" in (images[0]["alt"] or "")
    assert "1 opened" in (images[0]["alt"] or "")
    assert "GitHub lists workflow runs for about a year" in text
    boxes = [a for a in page.find("input") if a.get("type") == "checkbox"]
    assert {a.get("value") for a in boxes} == {"org_bot", "external_bot"}
    assert not any("checked" in a for a in boxes)


def test_missing_charts_and_data_are_named_not_linked(tmp_path: Path) -> None:
    site = _site(tmp_path, "acme", charts=False, export=False)
    text = (site / "acme" / "index.html").read_text(encoding="utf-8")
    assert not _Page(text).find("img")
    assert "github-statistics render" in text
    assert "github-statistics export" in text
    assert "data/" not in text


def test_downloads_link_export_documents_alone(tmp_path: Path) -> None:
    _seed(tmp_path / "cache", "acme", "acme/a", "alice")
    assert _run(tmp_path, "export", "acme").exit_code == 0
    data = tmp_path / "site" / "acme" / "data"
    exported = sorted(path.name for path in data.iterdir())
    (data / "contributors.csv").write_text("login\nstale\n", encoding="utf-8")
    (data / "purged.json").write_text("{}", encoding="utf-8")
    result = _run(tmp_path, "site", "acme")
    assert result.exit_code == 0, result.output
    text = (tmp_path / "site" / "acme" / "index.html").read_text(encoding="utf-8")
    links = [a["href"] or "" for a in _Page(text).find("a")]
    assert sorted(h[5:] for h in links if h.startswith("data/")) == exported


def test_single_target_site(tmp_path: Path) -> None:
    site = _site(tmp_path, "acme")
    assert sorted(p.relative_to(site).as_posix() for p in _pages(site)) == [
        "acme/index.html",
        "index.html",
    ]


def test_footer_date_comes_from_now_and_script_holds_no_data(tmp_path: Path) -> None:
    from github_statistics.config import Config, Target
    from github_statistics.config.models import Window
    from github_statistics.series import (
        build_leaderboard,
        build_repositories,
        build_series,
    )
    from github_statistics.site import SiteTarget, build_site
    from github_statistics.store import Store, store_path

    _seed(tmp_path / "cache", "acme", "acme/a", HOSTILE_LOGIN)
    target = Target("acme")
    config = Config(targets=(target,), window=Window(days=10))
    path = store_path(tmp_path / "cache", "api.github.com", "acme")
    with Store(path) as store:
        built = SiteTarget(
            target,
            build_series(store, config, target, until=UNTIL),
            build_leaderboard(store, config, target, until=UNTIL),
            build_repositories(store, config, target, until=UNTIL),
            merged=1,
        )
    written = build_site([built], config, tmp_path / "out", now=NOW)
    assert tmp_path / "out" / "acme" / "index.html" in written
    text = (tmp_path / "out" / "acme" / "index.html").read_text(encoding="utf-8")
    assert "2026-03-11" in text
    assert "06:30" not in text
    script = (tmp_path / "out" / "assets" / "site.js").read_text(encoding="utf-8")
    assert not script.startswith("\n")
    assert "SPDX" not in script
    assert "onerror" not in script
    assert "simpleDatatables.DataTable" in script


def test_membership_note_follows_the_identity_lists(tmp_path: Path) -> None:
    from github_statistics.config import Config, Identities, Target, resolve_identities
    from github_statistics.config.models import Window
    from github_statistics.series import (
        build_leaderboard,
        build_repositories,
        build_series,
    )
    from github_statistics.site import SiteTarget, build_site
    from github_statistics.store import Store, store_path

    _seed(tmp_path / "cache", "acme", "acme/a", "alice")
    target = Target(
        "acme", identities=Identities(members=("bob",), member_orgs=("acme", "labs"))
    )
    config = Config(targets=(target,), window=Window(days=10))
    identities = resolve_identities(target, config)
    path = store_path(tmp_path / "cache", "api.github.com", "acme")
    with Store(path) as store:
        built = SiteTarget(
            target,
            build_series(store, config, target, until=UNTIL),
            build_leaderboard(store, config, target, until=UNTIL),
            build_repositories(store, config, target, until=UNTIL),
            merged=1,
            identities=identities,
        )
    build_site([built], config, tmp_path / "out", now=NOW)
    text = (tmp_path / "out" / "acme" / "index.html").read_text(encoding="utf-8")
    assert "membership of acme or labs is public on GitHub" in text
    assert "configuration names as members" in text
    assert "bob" not in text
    assert "If acme is a user rather than an organisation" in text


def test_headline_counts_merges_by_deleted_accounts(tmp_path: Path) -> None:
    from github_statistics.store import Item, Store, store_path

    _seed(tmp_path / "cache", "acme", "acme/a", "alice")
    with Store(store_path(tmp_path / "cache", "api.github.com", "acme")) as store:
        store.upsert_items(
            [
                Item(
                    repo="acme/a",
                    number=2,
                    node_id="PR_ghost",
                    kind="pr",
                    created_at=_at(4),
                    closed_at=_at(5),
                    merged_at=_at(5),
                    author=None,
                    author_type=None,
                    updated_at=_at(5),
                )
            ]
        )
    result = _run(tmp_path, "site", "acme")
    assert result.exit_code == 0, result.output
    text = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "<dt>Pull requests opened</dt><dd>2</dd>" in text
    assert "<dt>Pull requests merged</dt><dd>2</dd>" in text


def test_scope_rule_names_both_include_and_exclude(tmp_path: Path) -> None:
    from github_statistics.config import Config, Target
    from github_statistics.config.models import Window
    from github_statistics.series import build_repositories, build_series
    from github_statistics.site import SiteTarget, build_site
    from github_statistics.store import Store, store_path

    _seed(tmp_path / "cache", "ACME", "ACME/a", "alice")
    target = Target("ACME", include=("a*",), exclude=("ab",))
    config = Config(targets=(target,), window=Window(days=10))
    path = store_path(tmp_path / "cache", "api.github.com", "ACME")
    with Store(path) as store:
        built = SiteTarget(
            target,
            build_series(store, config, target, until=UNTIL),
            [],
            build_repositories(store, config, target, until=UNTIL),
            merged=0,
        )
    build_site([built], config, tmp_path / "out", now=NOW)
    for page in ("index.html", "acme/index.html"):
        text = (tmp_path / "out" / page).read_text(encoding="utf-8")
        assert "Public repositories of ACME matching a*, except ab;" in text, page


def test_scope_rule_for_exact_names_drops_the_toggles(tmp_path: Path) -> None:
    from github_statistics.config import Config, Target
    from github_statistics.config.models import Window
    from github_statistics.series import build_repositories, build_series
    from github_statistics.site import SiteTarget, build_site
    from github_statistics.store import Store, store_path

    _seed(tmp_path / "cache", "acme", "acme/a", "alice")
    target = Target("acme", include=("a",))
    config = Config(targets=(target,), window=Window(days=10))
    path = store_path(tmp_path / "cache", "api.github.com", "acme")
    with Store(path) as store:
        built = SiteTarget(
            target,
            build_series(store, config, target, until=UNTIL),
            [],
            build_repositories(store, config, target, until=UNTIL),
            merged=0,
        )
    build_site([built], config, tmp_path / "out", now=NOW)
    for page in ("index.html", "acme/index.html"):
        text = (tmp_path / "out" / page).read_text(encoding="utf-8")
        assert "Public repositories of acme named a;" in text, page
        assert "left out" not in text, page


def test_landing_card_names_each_missing_chart(tmp_path: Path) -> None:
    _seed(tmp_path / "cache", "acme", "acme/a", "alice")
    _charts(tmp_path / "site", "acme")
    for stale in (tmp_path / "site" / "acme" / "img").glob("issues-all*.png"):
        stale.unlink()
    result = _run(tmp_path, "site", "acme")
    assert result.exit_code == 0, result.output
    text = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "No issues chart yet: run <code>github-statistics render</code>" in text
    assert "No pull requests chart yet" not in text
    assert len(_Page(text).find("img")) == len(METRICS) - 1


def test_landing_card_without_the_all_class_names_classes(tmp_path: Path) -> None:
    from github_statistics.config import Config, Target
    from github_statistics.config.models import Window
    from github_statistics.series import build_repositories, build_series
    from github_statistics.site import SiteTarget, build_site
    from github_statistics.store import Store, store_path

    _seed(tmp_path / "cache", "acme", "acme/a", "alice")
    _charts(tmp_path / "out", "acme")
    target = Target("acme")
    config = Config(targets=(target,), window=Window(days=10), classes=("member",))
    path = store_path(tmp_path / "cache", "api.github.com", "acme")
    with Store(path) as store:
        built = SiteTarget(
            target,
            build_series(store, config, target, until=UNTIL),
            [],
            build_repositories(store, config, target, until=UNTIL),
            merged=0,
        )
    build_site([built], config, tmp_path / "out", now=NOW)
    text = (tmp_path / "out" / "index.html").read_text(encoding="utf-8")
    assert "github-statistics render" not in text
    assert "add <code>all</code> to <code>classes</code>" in text
    assert not _Page(text).find("img")


def test_site_command_fails_without_a_store(tmp_path: Path) -> None:
    result = _run(tmp_path, "site", "acme")
    assert result.exit_code == 1
    assert "no store for acme" in result.output
    assert not (tmp_path / "site").exists()


def test_site_command_refuses_a_selection_narrower_than_the_store(
    tmp_path: Path,
) -> None:
    from github_statistics.cli import app

    _seed(tmp_path / "cache", "acme", "acme/a", "alice")
    result = runner.invoke(
        app,
        [
            "site",
            "--repo",
            "acme/b",
            "--days",
            "10",
            "--until",
            UNTIL.isoformat(),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output-dir",
            str(tmp_path / "site"),
        ],
    )
    assert result.exit_code == 1
    assert "acme/a" in result.output
    assert not (tmp_path / "site").exists()


def test_package_holds_every_template_and_asset() -> None:
    from importlib.resources import files

    from github_statistics.site import build

    package = files("github_statistics.site")
    for name in build.TEMPLATES:
        assert (package / "templates" / name).is_file(), name
    for name in build.ASSETS:
        assert (package / "assets" / name).is_file(), name
