# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the configuration schema, loader and identity resolution."""

from __future__ import annotations

import base64
import importlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest

IDENTITY_KEYS = ("org_bots", "external_bots", "members", "member_orgs", "ignore_logins")


def _doc(**extra: Any) -> dict[str, Any]:
    """A minimal valid configuration document, with ``extra`` merged in."""
    return {"targets": [{"owner": "acme"}], **extra}


def _raw(**extra: Any) -> str:
    """`_doc` as JSON text."""
    return json.dumps(_doc(**extra))


def test_schema_accepts_the_design_example() -> None:
    """Every key of the design's Appendix A example validates."""
    from github_statistics.config.schema import validate

    validate(
        {
            "version": 1,
            "identities": {key: [] for key in IDENTITY_KEYS},
            "targets": [
                {
                    "owner": "lfreleng-actions",
                    "token_env": "GITHUB_TOKEN",
                    "include": [],
                    "exclude": ["test-tags-*"],
                    "scan_archived": False,
                    "scan_forks": False,
                    "scan_templates": True,
                    "identities": {"org_bots": ["x[bot]"]},
                }
            ],
            "window": {"days": 365},
            "metrics": {
                "pull_requests": True,
                "issues": True,
                "workflows": True,
                "workflow_outcomes": False,
            },
            "classes": ["all", "member", "external", "org_bot", "external_bot", "bots"],
            "render": {
                "width": 3840,
                "height": 2160,
                "derivatives": [1920],
                "svg": False,
                "palette": {"opened": "#0072B2"},
                "back_on_tie": {
                    "pull_requests": "closed",
                    "issues": "closed",
                    "workflows": "jobs",
                },
            },
            "contributors": {"top_n": 100, "columns": ["merged_by"]},
            "budget": {"rest": 3000, "graphql_points": 1500, "reserve": 500},
            "output": {"directory": "site"},
            "cache": {"directory": None},
        }
    )


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ({"targets": [{"owner": "acme"}], "colour": "red"}, "unknown key 'colour'"),
        ({"targets": [{"owner": "acme", "scan_private": True}]}, "'scan_private'"),
        ({"targets": [{"owner": "acme"}], "window": {"dayz": 9}}, "unknown key 'dayz'"),
        (
            {
                "targets": [{"owner": "acme"}],
                "render": {"palette": {"merged": "#000000"}},
            },
            "unknown key 'merged'",
        ),
        (
            {"targets": [{"owner": "acme", "include": ["a"], "exclude": ["b"]}]},
            "include",
        ),
        ({"targets": [{"owner": "acme"}], "window": {"days": 6}}, "window.days"),
        ({"targets": [{"owner": "acme"}], "window": {"days": 731}}, "window.days"),
        (
            {"targets": [{"owner": "acme"}], "budget": {"graphql_points": 1}},
            "budget.graphql_points",
        ),
        ({"targets": []}, "targets"),
        ({}, "targets"),
        (
            {"targets": [{"owner": "acme"}, {"owner": "ACME", "include": ["b"]}]},
            r"targets\[1\]\.owner: repeats targets\[0\]\.owner",
        ),
    ],
)
def test_schema_rejects(document: dict[str, Any], reason: str) -> None:
    """Typos, private scanning, include with exclude and bad windows all fail."""
    from github_statistics.config import ConfigError
    from github_statistics.config.schema import validate

    with pytest.raises(ConfigError, match=reason) as caught:
        validate(document)
    assert "\n" not in str(caught.value)


TOKEN = "ghp_do_not_echo_this_value"


@pytest.mark.parametrize(
    "document",
    [
        {"targets": {"owner": "acme", "token_env": TOKEN}},
        {"targets": [{"owner": TOKEN}]},
        {"targets": [{"owner": "acme", "token_env": ["x", TOKEN]}]},
        {"targets": [{"owner": "acme", "include": [TOKEN], "exclude": [TOKEN]}]},
        {"targets": [{"owner": "acme"}], "version": TOKEN},
        {"targets": [{"owner": "acme"}], "classes": [TOKEN]},
        {"targets": [{"owner": "acme"}], "window": {"days": TOKEN}},
        {"targets": [{"owner": "acme"}], "render": {"palette": {"opened": TOKEN}}},
    ],
)
def test_schema_errors_never_echo_values(document: dict[str, Any]) -> None:
    """A rejected value, which may be a misplaced token, is never quoted."""
    from github_statistics.config import ConfigError
    from github_statistics.config.schema import validate

    with pytest.raises(ConfigError) as caught:
        validate(document)
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("days", [7, 730])
def test_schema_accepts_window_bounds(days: int) -> None:
    """The window may be anything from 7 to 730 days."""
    from github_statistics.config.schema import validate

    validate(_doc(window={"days": days}))


def test_schema_allows_include_with_empty_exclude() -> None:
    """Only both lists non-empty together is an error."""
    from github_statistics.config.schema import validate

    validate({"targets": [{"owner": "acme", "include": ["a"], "exclude": []}]})


def test_loads_applies_defaults() -> None:
    """A minimal document loads with the documented defaults."""
    from github_statistics.config import loads

    config = loads(_raw())

    target = config.targets[0]
    assert (target.owner, target.token_env, target.scan_templates) == (
        "acme",
        "GITHUB_TOKEN",
        True,
    )
    assert config.window.days == 365
    assert config.render.back_on_tie.workflows == "jobs"
    assert config.identities.org_bots is None


def test_budget_reserve_unset_unless_configured() -> None:
    """An omitted reserve stays unset, so the transport picks its default."""
    from github_statistics.config import loads

    assert loads(_raw()).budget.reserve is None
    assert loads(_raw(budget={"reserve": 500})).budget.reserve == 500


def test_loads_reads_base64_as_json() -> None:
    """Base64 of a document loads to the same configuration as the document."""
    from github_statistics.config import loads

    raw = _raw(identities={"org_bots": ["a[bot]"]}, window={"days": 30})
    encoded = base64.b64encode(raw.encode()).decode()

    assert loads(encoded) == loads(raw)


@pytest.mark.parametrize("raw", ["", "not base64 or json!", "{oops", "WzFd"])
def test_loads_rejects_garbage(raw: str) -> None:
    """Empty text, non-JSON and non-object documents raise `ConfigError`."""
    from github_statistics.config import ConfigError, loads

    with pytest.raises(ConfigError):
        loads(raw)


def test_dump_round_trips() -> None:
    """A dumped configuration loads back to an equal one."""
    from github_statistics.config import loads
    from github_statistics.config.loader import dump

    config = loads(
        _raw(
            identities={"members": []},
            render={"palette": {"opened": "#000000"}},
            contributors={"columns": ["closed_by"]},
        )
    )

    assert loads(json.dumps(dump(config))) == config


@pytest.mark.parametrize(
    "value",
    ["ghp_abc123", "github_pat_11AA", "ghs_x", "gho_y", " ghp_abc123", "\tghs_x\n"],
)
def test_token_shaped_token_env_warns(
    value: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A token in ``token_env`` warns without echoing the value."""
    from github_statistics.config import loads

    with caplog.at_level(logging.WARNING):
        loads(json.dumps({"targets": [{"owner": "acme", "token_env": value}]}))

    assert "token_env" in caplog.text
    assert value not in caplog.text


def test_variable_name_token_env_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ordinary variable name loads silently."""
    from github_statistics.config import loads

    with caplog.at_level(logging.WARNING):
        loads(json.dumps({"targets": [{"owner": "acme", "token_env": "MY_TOKEN"}]}))

    assert caplog.text == ""


def test_load_file_names_the_file_on_failure(tmp_path: Path) -> None:
    """Missing and invalid files raise `ConfigError` naming the path."""
    from github_statistics.config import ConfigError, load_file

    missing = tmp_path / "missing.json"
    with pytest.raises(ConfigError, match="missing.json"):
        load_file(missing)
    broken = tmp_path / "broken.json"
    broken.write_text('{"targets": []}')
    with pytest.raises(ConfigError, match="broken.json"):
        load_file(broken)


def test_default_paths_follow_xdg() -> None:
    """XDG variables win; otherwise the home directory's dot directories."""
    from github_statistics.config import default_cache_dir, default_config_path

    xdg = {"XDG_CONFIG_HOME": "/x/conf", "XDG_CACHE_HOME": "/x/cache", "HOME": "/h"}
    home = {"HOME": "/h"}

    assert default_config_path(xdg) == Path("/x/conf/github-statistics/config.json")
    assert default_cache_dir(xdg) == Path("/x/cache/github-statistics")
    assert default_config_path(home) == Path("/h/.config/github-statistics/config.json")
    assert default_cache_dir(home) == Path("/h/.cache/github-statistics")


def _sources(tmp_path: Path) -> dict[str, Any]:
    """One configuration per source, each naming a different owner."""
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({"targets": [{"owner": "from-path"}]}))
    default = tmp_path / "xdg" / "github-statistics" / "config.json"
    default.parent.mkdir(parents=True)
    default.write_text(json.dumps({"targets": [{"owner": "from-default"}]}))
    env_raw = json.dumps({"targets": [{"owner": "from-env"}]})
    return {
        "config_data": json.dumps({"targets": [{"owner": "from-data"}]}),
        "config_path": explicit,
        "env": {"GHS_CONFIG": env_raw, "XDG_CONFIG_HOME": str(tmp_path / "xdg")},
    }


@pytest.mark.parametrize(
    ("drop", "source", "owner"),
    [
        ((), "config-data", "from-data"),
        (("config_data",), "config", "from-path"),
        (("config_data", "config_path"), "env", "from-env"),
        (("config_data", "config_path", "GHS_CONFIG"), "default", "from-default"),
    ],
)
def test_resolve_source_precedence(
    tmp_path: Path, drop: tuple[str, ...], source: str, owner: str
) -> None:
    """Each source wins over every source below it, and is reported."""
    from github_statistics.config import resolve

    sources = _sources(tmp_path)
    env = dict(sources.pop("env"))
    for key in drop:
        if key in sources:
            del sources[key]
        else:
            del env[key]

    resolution = resolve(env=env, **sources)

    assert resolution.source == source
    assert [t.owner for t in resolution.config.targets] == [owner]


def test_resolve_from_flags_alone(tmp_path: Path) -> None:
    """With no configuration, ``--org`` builds a one-target configuration."""
    from github_statistics.config import Overrides, resolve

    resolution = resolve(
        overrides=Overrides(orgs=("acme",)), env={"HOME": str(tmp_path)}
    )

    assert resolution.source == "flags"
    assert [t.owner for t in resolution.config.targets] == ["acme"]


def test_resolve_without_any_source_fails(tmp_path: Path) -> None:
    """No configuration and no ``--org``/``--repo`` is an error."""
    from github_statistics.config import ConfigError, resolve

    with pytest.raises(ConfigError, match="no configuration"):
        resolve(env={"HOME": str(tmp_path)})


def test_resolve_org_selects_configured_targets() -> None:
    """``--org`` keeps a configured target's settings and adds unknown owners."""
    from github_statistics.config import Overrides, resolve

    raw = json.dumps(
        {"targets": [{"owner": "acme", "token_env": "ACME"}, {"owner": "other"}]}
    )

    config = resolve(config_data=raw, overrides=Overrides(orgs=("acme", "new"))).config

    assert [(t.owner, t.token_env) for t in config.targets] == [
        ("acme", "ACME"),
        ("new", "GITHUB_TOKEN"),
    ]


@pytest.mark.parametrize(
    "overrides",
    [{"orgs": ("acme",)}, {"repos": ("acme/alpha",)}, {}],
)
def test_resolve_rejects_two_targets_for_one_owner(overrides: dict[str, Any]) -> None:
    """A second target for an owner fails before ``--org``/``--repo`` pick one."""
    from github_statistics.config import ConfigError, Overrides, resolve

    raw = json.dumps(
        {
            "targets": [
                {"owner": "acme", "include": ["alpha"]},
                {"owner": "acme", "include": ["beta"], "token_env": "BETA"},
            ]
        }
    )

    with pytest.raises(ConfigError, match="one target per owner"):
        resolve(config_data=raw, overrides=Overrides(**overrides))


def test_resolve_repo_builds_an_include_list(tmp_path: Path) -> None:
    """``--repo`` builds one target per owner with exactly those names."""
    from github_statistics.config import Overrides, resolve

    overrides = Overrides(repos=("acme/a", "acme/b", "beta/c"))
    config = resolve(overrides=overrides, env={"HOME": str(tmp_path)}).config

    assert [(t.owner, t.include) for t in config.targets] == [
        ("acme", ("a", "b")),
        ("beta", ("c",)),
    ]


@pytest.mark.parametrize(
    "repo", ["acme", "acme/", "/a", "a/b/c", "acme/*", "acme/repo?", "acme/[ab]"]
)
def test_resolve_rejects_malformed_repo(tmp_path: Path, repo: str) -> None:
    """``--repo`` must be OWNER/NAME."""
    from github_statistics.config import ConfigError, Overrides, resolve

    with pytest.raises(ConfigError, match="OWNER/NAME"):
        resolve(overrides=Overrides(repos=(repo,)), env={"HOME": str(tmp_path)})


def test_resolve_applies_and_validates_overrides() -> None:
    """Flag overrides replace configured values and are validated."""
    from github_statistics.config import ConfigError, Overrides, resolve

    raw = _raw(window={"days": 30})
    overrides = Overrides(
        include=("x-*",), days=90, output_dir="out", budget=10, token_env="T"
    )

    config = resolve(config_data=raw, overrides=overrides).config

    assert config.targets[0].include == ("x-*",)
    assert config.targets[0].token_env == "T"
    assert (config.window.days, config.output.directory, config.budget.rest) == (
        90,
        "out",
        10,
    )
    with pytest.raises(ConfigError, match=r"window\.days"):
        resolve(config_data=raw, overrides=Overrides(days=3))


@pytest.mark.xfail(strict=True)
def test_budget_minutes_validates_and_the_flag_overrides_it() -> None:
    """``budget.minutes`` is unset by default, at least 1, and the flag wins."""
    config: Any = importlib.import_module("github_statistics.config")

    assert config.loads(_raw()).budget.minutes is None
    assert config.loads(_raw(budget={"minutes": 30})).budget.minutes == 30
    with pytest.raises(config.ConfigError, match=r"budget\.minutes"):
        config.loads(_raw(budget={"minutes": 0}))
    overrides = config.Overrides(max_minutes=5)
    raw = _raw(budget={"minutes": 30})
    resolved = config.resolve(config_data=raw, overrides=overrides).config
    assert resolved.budget.minutes == 5


@pytest.mark.parametrize(
    ("overrides", "flag"),
    [
        ({"include": ("*",), "exclude": ("private-*",)}, "--include"),
        ({"repos": ("acme/one",), "include": ("*",)}, "--repo"),
        ({"repos": ("acme/one",), "exclude": ("x",)}, "--repo"),
    ],
)
def test_resolve_rejects_conflicting_overrides(
    overrides: dict[str, Any], flag: str
) -> None:
    """``resolve`` refuses the combinations the command line refuses."""
    from github_statistics.config import ConfigError, Overrides, resolve

    with pytest.raises(ConfigError, match=f"{flag} cannot be combined"):
        resolve(config_data=_raw(), overrides=Overrides(**overrides))


def test_split_list_accepts_every_separator() -> None:
    """Commas, spaces and newlines all separate; repeats concatenate."""
    from github_statistics.config.loader import split_list

    assert split_list(["a[bot], b[bot]\nc", "d  e,,"]) == (
        "a[bot]",
        "b[bot]",
        "c",
        "d",
        "e",
    )
    assert split_list([""]) == ()
    assert split_list(None) == ()


_LEVELS = ("cli", "target", "config", "default")


@pytest.mark.parametrize("key", IDENTITY_KEYS)
@pytest.mark.parametrize("highest", _LEVELS)
def test_identity_precedence(key: str, highest: str) -> None:
    """Per key, the highest level that sets it wins, and replaces the rest."""
    from github_statistics.config import Identities, loads, resolve_identities

    values = {level: [f"{level}-login"] for level in _LEVELS}
    present = _LEVELS[_LEVELS.index(highest) :]
    document: dict[str, Any] = {"targets": [{"owner": "acme"}]}
    if "target" in present:
        document["targets"][0]["identities"] = {key: values["target"]}
    if "config" in present:
        document["identities"] = {key: values["config"]}
    config = loads(json.dumps(document))
    cli = Identities(**{key: tuple(values["cli"])}) if "cli" in present else None

    resolved = getattr(resolve_identities(config.targets[0], config, cli), key)

    assert resolved.source == highest
    if highest == "default":
        assert resolved.values == (("acme",) if key == "member_orgs" else ())
    else:
        assert resolved.values == tuple(values[highest])


def test_identity_levels_replace_not_merge() -> None:
    """A level's list is the whole list; other keys are left alone."""
    from github_statistics.config import loads, resolve_identities

    config = loads(
        json.dumps(
            {
                "identities": {"org_bots": ["a", "b"], "members": ["m"]},
                "targets": [{"owner": "acme", "identities": {"org_bots": []}}],
            }
        )
    )

    resolved = resolve_identities(config.targets[0], config)

    assert (resolved.org_bots.values, resolved.org_bots.source) == ((), "target")
    assert (resolved.members.values, resolved.members.source) == (("m",), "config")


def test_empty_member_orgs_means_the_owner() -> None:
    """An explicitly empty ``member_orgs`` resolves to the target owner."""
    from github_statistics.config import loads, resolve_identities

    config = loads(_raw(identities={"member_orgs": []}))

    resolved = resolve_identities(config.targets[0], config).member_orgs

    assert (resolved.values, resolved.source) == (("acme",), "config")


def test_resolve_token_reads_the_named_variable() -> None:
    """The token comes from the environment variable the target names."""
    from github_statistics.config import loads, resolve_token

    target = loads(
        json.dumps({"targets": [{"owner": "a", "token_env": "MY_T"}]})
    ).targets[0]

    assert resolve_token(target, {"MY_T": "secret"}) == "secret"
    assert resolve_token(target, {"GITHUB_TOKEN": "other"}) is None
    assert resolve_token(target, {"MY_T": ""}) is None
