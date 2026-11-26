<!--
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
-->

# 📊 GitHub Statistics

<!-- prettier-ignore-start -->
<!-- markdownlint-disable-next-line MD013 -->
[![Linux Foundation](https://img.shields.io/badge/Linux-Foundation-blue)](https://linuxfoundation.org/) [![Source Code](https://img.shields.io/badge/GitHub-100000?logo=github&logoColor=white&color=blue)](https://github.com/lfreleng-actions/github-statistics-action) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lfreleng-actions/github-statistics-action/badge)](https://scorecard.dev/viewer/?uri=github.com/lfreleng-actions/github-statistics-action)
<!-- prettier-ignore-end -->

Daily, visual activity statistics for a GitHub organisation, a set of
its repositories, or a single repository. Pull requests, issues and CI
each get a 3D bar chart with one bar per day across a configurable
window, with contributor leaderboards beneath. A Python command line
tool generates the output as a GitHub Pages site, and a thin composite
action wraps it for use in workflows.

## 🗒️ Published reports

<https://lfreleng-actions.github.io/github-statistics-action/>

The [daily workflow](.github/workflows/statistics.yaml) rebuilds the
site at 05:00 UTC; see [Secrets and variables](#secrets-and-variables)
for its settings.

## Usage

```yaml
permissions:
  actions: read
  checks: read
  contents: read
  issues: read
  pull-requests: read

steps:
  - uses: lfreleng-actions/github-statistics-action@<commit-sha>  # vX.Y.Z
    id: statistics
    with:
      org: lfreleng-actions
      token: ${{ steps.app-token.outputs.token }}
      config: ${{ secrets.GHS_CONFIG || vars.GHS_CONFIG }}
      org_bots: ${{ vars.GHS_ORG_BOTS }}
      output_dir: site
```

The action runs `github-statistics run`: for each target it clears
the target's `img/` and `data/` directories, collects, renders the
charts, writes the JSON exports and the target's pages, then writes
the landing page. `command: scope` instead resolves the public
repositories in scope and publishes them as outputs.

The action installs the release named in
`.github/runtime-pin/requirements.txt`, which Dependabot keeps
current. `tool_version` overrides the pin; pull requests and
`use_local_source: "true"` run the action's own source instead.

### Inputs

Every input is optional and defaults to the empty string. The action
passes a flag to the tool when its input holds a value, so an older pinned
release never receives a flag it does not know. List inputs accept
commas, spaces or newlines between items.

<!-- markdownlint-disable MD013 -->

| Input              | Description                                                                      |
| ------------------ | -------------------------------------------------------------------------------- |
| `command`          | `run` (default) or `scope`                                                       |
| `config`           | Configuration as JSON, raw or base64; see [Configuration](#configuration)        |
| `org`              | Owner to report on: shorthand for a one-target configuration                     |
| `repositories`     | Repository names or globs to keep                                                |
| `exclude`          | Repository names or globs to drop; refused together with `repositories`          |
| `repo`             | `owner/name` for repository scope                                                |
| `token`            | Token to read GitHub with (default: the job's `github.token`)                    |
| `days`             | Window length in days                                                            |
| `until`            | Last day of the window, `YYYY-MM-DD` (default: yesterday, UTC)                   |
| `output_dir`       | Site root (default `site`)                                                       |
| `cache_dir`        | Store root (default `~/.cache/github-statistics`)                                |
| `budget`           | REST requests this run may spend                                                 |
| `max_minutes`      | Minutes collection may take (default: no limit); the stages after it still run   |
| `refresh`          | `true` to ignore watermarks and re-read the window (`run`)                       |
| `strict`           | `true` to exit 2 when a repository was unreadable (`run`)                        |
| `org_bots`         | The organisation's own bots; replaces the configured list                        |
| `external_bots`    | Bots from outside the organisation; replaces the configured list                 |
| `members`          | Humans counted as members; replaces the configured list                          |
| `member_orgs`      | Organisations whose public members count; replaces the configured list           |
| `ignore_logins`    | Accounts left out of every output; replaces the configured list                  |
| `tool_version`     | `github-statistics` release to run instead of the runtime pin                    |
| `use_local_source` | `true` to run the action's own source (pull requests always do)                  |

<!-- markdownlint-enable MD013 -->

The action carries the token as `GITHUB_TOKEN`, so a configuration used
with the action leaves `token_env` at its default. With
`command: scope`, setting `refresh` or `strict` to `true` fails:
neither applies to `scope`.

### Outputs

<!-- markdownlint-disable MD013 -->

| Output              | Command | Description                                                    |
| ------------------- | ------- | -------------------------------------------------------------- |
| `site_dir`          | `run`   | Directory holding the site                                     |
| `requests_used`     | `run`   | REST requests the run spent                                    |
| `coverage_complete` | `run`   | `true` when every day of every metric is complete              |
| `gap_days`          | `run`   | Days the charts show as gaps, summed over targets              |
| `unclassified_bots` | `run`   | Comma-separated bots that no identity list names               |
| `repositories`      | `scope` | Comma-separated public repositories in scope                   |
| `repository_count`  | `scope` | Number of public repositories in scope                         |

<!-- markdownlint-enable MD013 -->

The run also appends its collection and coverage tables and the site
location to the job summary, and warns about each unclassified bot with the configuration
keys to add it to.

### Exit codes

| Code | Meaning                                                    |
| ---- | ---------------------------------------------------------- |
| 0    | Success; a spent budget still renders and builds the site  |
| 1    | Invalid configuration, or another failure                  |
| 2    | `strict` set and a repository was unreadable               |
| 3    | GitHub rejected the token; the run stops before rendering  |

### Token permissions

The tool reads and never writes. Grant read access alone:

<!-- markdownlint-disable MD013 -->

| Scope           | Token                                                                                                                    | Read permissions                                 |
| --------------- | ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------ |
| Organisation    | App installation token naming the resolved public repositories, or a fine-grained PAT limited to public repositories     | metadata, issues, pull requests, actions, checks |
| Repository list | Same, for those repositories                                                                                             | same                                             |
| Single repo     | The job's `GITHUB_TOKEN`                                                                                                 | same, through the job's `permissions`            |

<!-- markdownlint-enable MD013 -->

Never grant write or organisation permissions. A `401` stops the run
with exit code 3; a `403` on one repository marks it unreadable for
the run, and the coverage table says so.

## Configuration

The `config` input, or `--config-data` on the command line, takes the
configuration as JSON, raw or base64; `--config` takes a file. A
complete example (design Appendix A):

```json
{
  "version": 1,
  "identities": {
    "org_bots": [
      "lf-releng-issues-triage-bot[bot]",
      "lf-releng-code-monkey-bot[bot]",
      "lf-releng-code-review-bot[bot]"
    ],
    "external_bots": [
      "dependabot[bot]",
      "pre-commit-ci[bot]",
      "copilot-pull-request-reviewer[bot]",
      "Copilot",
      "github-actions[bot]",
      "sonarqubecloud[bot]"
    ],
    "members": [],
    "member_orgs": [],
    "ignore_logins": []
  },
  "targets": [
    {
      "owner": "lfreleng-actions",
      "token_env": "GITHUB_TOKEN",
      "exclude": ["test-tags-*"],
      "scan_archived": false,
      "scan_forks": false,
      "scan_templates": true,
      "identities": {}
    }
  ],
  "window": { "days": 365 },
  "metrics": {
    "pull_requests": true,
    "issues": true,
    "workflows": true,
    "workflow_outcomes": false
  },
  "classes": ["all", "member", "external", "org_bot", "external_bot"],
  "render": {
    "width": 3840,
    "height": 2160,
    "derivatives": [1920],
    "svg": false,
    "palette": {},
    "back_on_tie": {
      "pull_requests": "closed",
      "issues": "closed",
      "workflows": "jobs"
    }
  },
  "contributors": { "top_n": 100, "columns": [] },
  "budget": { "rest": 3000, "graphql_points": 1500, "reserve": 500 },
  "output": { "directory": "site" },
  "cache": { "directory": null }
}
```

### Identities

Each identity list is available at every level:

<!-- markdownlint-disable MD013 -->

| Key            | Config JSON (`identities.*`) | CLI               | Action input    | Variable for the action input |
| -------------- | ---------------------------- | ----------------- | --------------- | ----------------------------- |
| Our bots       | `org_bots`                   | `--org-bots`      | `org_bots`      | `vars.GHS_ORG_BOTS`           |
| External bots  | `external_bots`              | `--external-bots` | `external_bots` | `vars.GHS_EXTERNAL_BOTS`      |
| Extra members  | `members`                    | `--members`       | `members`       | `vars.GHS_MEMBERS`            |
| Member orgs    | `member_orgs`                | `--member-orgs`   | `member_orgs`   | `vars.GHS_MEMBER_ORGS`        |
| Ignored logins | `ignore_logins`              | `--ignore-logins` | `ignore_logins` | `vars.GHS_IGNORE_LOGINS`      |

<!-- markdownlint-enable MD013 -->

A level that sets a key replaces that key's list from the levels
below it. Precedence, highest first: command line flags and the action
inputs that become them; `targets[].identities` for that target;
top-level `identities`; the built-in defaults, which are empty lists
and `member_orgs` equal to the target owner.

## Secrets and variables

`.github/workflows/statistics.yaml` collects, renders and deploys the
published site every day, and on `workflow_dispatch` with optional
`repositories`, `refresh` and `use_local_source` inputs. It reads
these from the `production` environment, the repository or the
organisation, and the nearest one of a name wins:

<!-- markdownlint-disable MD013 -->

| Name                        | Kind               | Purpose                                                                         |
| --------------------------- | ------------------ | ------------------------------------------------------------------------------- |
| `LF_RELENG_BOT_CLIENT_ID`   | Variable           | Client ID of the organisation App that mints the read token                     |
| `LF_RELENG_BOT_PRIVATE_KEY` | Secret             | Private key of that App                                                         |
| `GHS_CONFIG`                | Secret or variable | Configuration JSON, raw or base64; the secret wins when both exist              |
| `GHS_STORE_KEY`             | Secret             | Passphrase that encrypts the store in the cache and the artifact                |
| `GHS_ORG_BOTS`              | Variable           | Feeds the `org_bots` input                                                      |
| `GHS_EXTERNAL_BOTS`         | Variable           | Feeds the `external_bots` input                                                 |
| `GHS_MEMBERS`               | Variable           | Feeds the `members` input                                                       |
| `GHS_MEMBER_ORGS`           | Variable           | Feeds the `member_orgs` input                                                   |
| `GHS_IGNORE_LOGINS`         | Variable           | Feeds the `ignore_logins` input                                                 |
| `SLACK_BOT_TOKEN`           | Secret             | Bot token that posts the failure notice                                         |
| `SLACK_CHANNEL_ID`          | Variable           | Channel for the failure notice                                                  |

<!-- markdownlint-enable MD013 -->

Every one is optional except the App pair, which the workflow needs
whenever the scope holds a repository. The action passes its token to
the tool as `GITHUB_TOKEN`, so a `GHS_CONFIG` must leave each target's
`token_env` at its default. The workflow sets the store root itself:
a `cache.directory` in `GHS_CONFIG` has no effect there. Without
`GHS_STORE_KEY` the workflow neither restores nor saves the store and
says so in a notice; each run then backfills from empty. Without both
Slack settings the workflow sends no failure notice and warns instead.

The App token names the public repositories the scope resolves to,
with read access alone and no organisation permission, so it cannot
read a private repository. An App token names at most 500
repositories: an organisation with more fails the run, and needs a
fine-grained personal access token limited to public repositories
instead. The workflow restores the store from the Actions cache, or
on a miss from the newest `github-statistics-store` artifact uploaded
by its own scheduled or dispatched runs on the default branch.

Before the first run, set the repository's Pages source to GitHub
Actions.

## Running locally

[uv](https://docs.astral.sh/uv/) runs a published release without
installing it:

```bash
export GITHUB_TOKEN="$(gh auth token)"
uvx github-statistics run --repo lfreleng-actions/github-statistics-action \
  --budget 200 --output-dir site
uvx github-statistics scope --org lfreleng-actions --format csv
```

Open `site/index.html` in a browser to view the result.

`--max-minutes N` stops collection `N` minutes after the command
starts, as a spent budget does: the run exits 0 and still renders,
exports and builds the site, and the next run resumes. It overrides
`budget.minutes`, which sets no time limit by default.

### Logging

Every command logs its progress to stderr at `INFO`: each `collect`
step as it starts and ends, with the REST requests and GraphQL points
spent and left, and a line per repository per step with what it read
and stored. Timestamps are in UTC. Set `--log-level` (`debug`, `info`,
`warning` or `error`) before the command, or `GHS_LOG_LEVEL`; the
option wins. Log lines never carry a token. The summary tables stay on
stdout.

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

CI runs `build-test.yaml` on pull requests, and `build-test-release.yaml`
builds, tests and publishes to TestPyPI then PyPI on a tag push.
