# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Configuration: the schema, its models, and the loader that finds and resolves it."""

from __future__ import annotations

from .loader import (
    Overrides,
    Resolution,
    default_cache_dir,
    default_config_path,
    load_file,
    loads,
    resolve,
    resolve_identities,
    resolve_token,
)
from .models import Config, Identities, ResolvedIdentities, Target
from .schema import ConfigError

__all__ = [
    "Config",
    "ConfigError",
    "Identities",
    "Overrides",
    "Resolution",
    "ResolvedIdentities",
    "Target",
    "default_cache_dir",
    "default_config_path",
    "load_file",
    "loads",
    "resolve",
    "resolve_identities",
    "resolve_token",
]
