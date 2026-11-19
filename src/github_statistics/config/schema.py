# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The JSON Schema for the configuration document, and its validation.

Every object sets ``additionalProperties: false``, so a misspelt key
fails loudly instead of being ignored. There is deliberately no
``scan_private`` key: private repositories are out of scope by design.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, best_match

IDENTITY_KEYS = ("org_bots", "external_bots", "members", "member_orgs", "ignore_logins")
CLASS_KEYS = ("all", "member", "external", "org_bot", "external_bot", "humans", "bots")
CONTRIBUTOR_COLUMNS = ("merged_by", "closed_by")
PALETTE_KEYS = ("opened", "closed", "runs", "jobs", "succeeded", "failed")
WINDOW_MIN_DAYS = 7
WINDOW_MAX_DAYS = 730

_STRINGS: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
}

_IDENTITIES: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": dict.fromkeys(IDENTITY_KEYS, _STRINGS),
}

_TARGET: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["owner"],
    "properties": {
        "owner": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9-]*$"},
        "token_env": {"type": "string", "minLength": 1},
        "include": _STRINGS,
        "exclude": _STRINGS,
        "scan_archived": {"type": "boolean"},
        "scan_forks": {"type": "boolean"},
        "scan_templates": {"type": "boolean"},
        "identities": {"$ref": "#/$defs/identities"},
    },
    # include and exclude may not both be non-empty (design §5).
    "not": {
        "required": ["include", "exclude"],
        "properties": {
            "include": {"minItems": 1},
            "exclude": {"minItems": 1},
        },
    },
}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    """A closed object schema: only the named keys are allowed."""
    return {"type": "object", "additionalProperties": False, "properties": properties}


SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "github-statistics configuration",
    "type": "object",
    "additionalProperties": False,
    "required": ["targets"],
    "$defs": {"identities": _IDENTITIES},
    "properties": {
        "version": {"const": 1},
        "identities": {"$ref": "#/$defs/identities"},
        "targets": {"type": "array", "minItems": 1, "items": _TARGET},
        "window": _object(
            {
                "days": {
                    "type": "integer",
                    "minimum": WINDOW_MIN_DAYS,
                    "maximum": WINDOW_MAX_DAYS,
                },
            }
        ),
        "metrics": _object(
            dict.fromkeys(
                ("pull_requests", "issues", "workflows", "workflow_outcomes"),
                {"type": "boolean"},
            )
        ),
        "classes": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"enum": list(CLASS_KEYS)},
        },
        "render": _object(
            {
                "width": {"type": "integer", "minimum": 1},
                "height": {"type": "integer", "minimum": 1},
                "derivatives": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                },
                "svg": {"type": "boolean"},
                "palette": _object(
                    dict.fromkeys(
                        PALETTE_KEYS,
                        {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
                    )
                ),
                "back_on_tie": _object(
                    {
                        "pull_requests": {"enum": ["opened", "closed"]},
                        "issues": {"enum": ["opened", "closed"]},
                        "workflows": {"enum": ["runs", "jobs"]},
                    }
                ),
            }
        ),
        "contributors": _object(
            {
                "top_n": {"type": "integer", "minimum": 0},
                "columns": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"enum": list(CONTRIBUTOR_COLUMNS)},
                },
            }
        ),
        "budget": _object(
            {
                "rest": {"type": "integer", "minimum": 1},
                "graphql_points": {"type": "integer", "minimum": 0},
                "reserve": {"type": ["integer", "null"], "minimum": 0},
            }
        ),
        "output": _object({"directory": {"type": "string", "minLength": 1}}),
        "cache": _object({"directory": {"type": ["string", "null"], "minLength": 1}}),
    },
}

_VALIDATOR = Draft202012Validator(SCHEMA)


class ConfigError(Exception):
    """The configuration cannot be found, read, parsed or validated."""


class NoConfigurationError(ConfigError):
    """No configuration source exists: no flag, no ``$GHS_CONFIG``, no default file."""


def _location(error: ValidationError) -> str:
    """Render the path to a failing value as ``targets[0].window``."""
    parts: list[str] = []
    for part in error.absolute_path:
        if isinstance(part, int):
            parts.append(f"[{part}]")
        else:
            parts.append(f".{part}" if parts else str(part))
    return "".join(parts) or "configuration"


def _plural(count: Any, noun: str) -> str:
    """``1 item`` or ``2 items``."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _expected(value: Any) -> str:
    """Name an expected JSON type, or a choice of types."""
    if isinstance(value, list):
        return " or ".join(str(item) for item in value)
    return str(value)


def _message(error: ValidationError) -> str:
    """Turn one validation error into a one-line reason.

    The reason comes from the failing constraint alone and never quotes
    the rejected value, which may be a token pasted in the wrong place.
    Unknown keys are named, since a misspelt key is the point of the error.
    """
    validator = error.validator
    value: Any = error.validator_value
    if validator == "not":
        return "include and exclude may not both be non-empty"
    if validator == "additionalProperties" and isinstance(error.instance, dict):
        schema = error.schema
        known = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
        unknown = sorted(key for key in error.instance if key not in known)
        return "unknown key " + ", ".join(repr(key) for key in unknown)
    if validator == "required" and isinstance(error.instance, dict):
        missing = [key for key in value if key not in error.instance]
        return "missing required key " + ", ".join(repr(key) for key in missing)
    match validator:
        case "type":
            return f"expected {_expected(value)}"
        case "enum":
            return "expected one of " + ", ".join(repr(item) for item in value)
        case "const":
            return f"expected {value!r}"
        case "pattern":
            return f"does not match {value}"
        case "minLength":
            return f"must be at least {_plural(value, 'character')}"
        case "minimum":
            return f"must be at least {value}"
        case "maximum":
            return f"must be at most {value}"
        case "minItems":
            return f"must have at least {_plural(value, 'item')}"
        case "uniqueItems":
            return "must not repeat an item"
    return f"fails the {validator} constraint"


def validate(data: Any) -> None:
    """Validate a parsed configuration document; raise `ConfigError` on failure.

    The error names the most relevant failing location and its reason
    on one line, so ``config check`` can print it as it is.
    """
    error = best_match(_VALIDATOR.iter_errors(data))
    if error is not None:
        raise ConfigError(f"{_location(error)}: {_message(error)}")
    _check_owners(data["targets"])


def _check_owners(targets: list[dict[str, Any]]) -> None:
    """Reject a second target for an owner, compared case-insensitively.

    Each owner gets one page, one cache and one set of target settings,
    and ``--org``/``--repo`` select a target by owner, so a second target
    for the same owner would silently replace the first.
    """
    seen: dict[str, int] = {}
    for index, target in enumerate(targets):
        first = seen.setdefault(target["owner"].lower(), index)
        if first != index:
            raise ConfigError(
                f"targets[{index}].owner: repeats targets[{first}].owner; "
                "use one target per owner"
            )
