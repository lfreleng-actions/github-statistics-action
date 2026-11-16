<!--
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
-->

# 📊 GitHub Statistics

<!-- prettier-ignore-start -->
<!-- markdownlint-disable-next-line MD013 -->
[![Linux Foundation](https://img.shields.io/badge/Linux-Foundation-blue)](https://linuxfoundation.org/) [![Source Code](https://img.shields.io/badge/GitHub-100000?logo=github&logoColor=white&color=blue)](https://github.com/lfreleng-actions/github-statistics-action) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![pre-commit.ci status badge]][pre-commit.ci results page] [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lfreleng-actions/github-statistics-action/badge)](https://scorecard.dev/viewer/?uri=github.com/lfreleng-actions/github-statistics-action)
<!-- prettier-ignore-end -->

Daily, visual activity statistics for a GitHub organisation, a set of
its repositories, or a single repository. Pull requests, issues and CI
each get a 3D bar chart with one bar per day across a configurable
window, with contributor leaderboards beneath. A Python command line
tool generates the output as a GitHub Pages site, and a thin composite
action wraps it for use in workflows.

## 🗒️ Published reports

The link to the published site will appear here once the site goes live.

## Status

The project is at the design stage: the
[design document](docs/DESIGN.md) describes the tool, the action and
the site, and records the decisions taken. The Python package and a
command line skeleton that prints its version exist; the action has
no inputs yet.

The next step is the rest of phases 1 and 2 of the design's delivery
plan (§13): a rendering spike on synthetic data, then the
configuration, store and release pipeline.

## Development

The project uses [uv](https://docs.astral.sh/uv/) and needs Python
3.11 or later. Install the locked dependencies and run the tests:

```bash
uv sync
uv run pytest
uv run github-statistics --version
```

Install the commit hooks with [prek](https://prek.j178.dev/), then
run them against the files a change touches:

```bash
prek install -t pre-commit -t commit-msg
prek run --files <files>
```

[pre-commit.ci results page]: https://results.pre-commit.ci/latest/github/lfreleng-actions/github-statistics-action/main
[pre-commit.ci status badge]: https://results.pre-commit.ci/badge/github/lfreleng-actions/github-statistics-action/main.svg
