# Shadow state digests

`plangraph state wait --role ROLE` scans every 60 seconds. Use `--once` to
read a single scan. Set `PLANGRAPH_STATE_CONFIG` to select a configuration or
create `~/.config/plangraph/state-shadow/ROLE.json`. The collector validates
the keeper token with `whoami` and retains that identity for its lifetime.

The operator supplies the following JSON fields. Configure each role with its
own authorized sources and keep the configuration directory writable only by
the collector owner.

| Field | Value |
| --- | --- |
| `role` | Role named on the command line |
| `principal` | Keeper principal name authenticated by `PG_TOKEN_FILE` |
| `directory` | Private parent directory for role event files and digests |
| `inbox` | The role's own newline-terminated, append-only inbox |
| `transcript` | Path to the role's active Claude JSONL transcript |
| `session_id` | Session UUID, matching the transcript filename stem |
| `data_repo` | Local data checkout with a refreshed `origin/main` |
| `decision_base` | Initial full commit ID on the history of data `origin/main` |
| `github_repo` | Authorized repository in owner/repo format |
| `github_since` | Initial lower bound for main runs, as an RFC 3339 instant |
| `pull_requests` | Optional list of authorized held or train PR numbers |
| `item_keys` | Optional role-authorized keys for decision rows |
| `quota` | Role-visible JSON file with live quota levels |
| `lease_margin_seconds` | Optional lead time for lease warnings, default 600 |

The service reads GitHub via `gh` and spawner host history via
`plangraph-spawn`. Keep both on its PATH. Enable the Rust lifecycle publisher in
legacy spawner and watchdog environments before starting shadow observation.
Set `PLANGRAPH_LIFECYCLE_PUBLISHER` to the installed `pg-spawnd` binary. The initial decision and GitHub
positions should cover the shadow observation window. The source checkout
synchronizer refreshes `origin/main`. Configure the next transcript path on
session replacement. Decision rows are included when they address the role or
reference one of its explicitly scoped item keys. Run transitions are included
when the run's role matches the collector or its item belongs to a project
authorized by the current keeper identity. A router identity covers every run.
Only messages addressed to the role or principal are written from the keeper
message feed.

The role subdirectory contains `events.jsonl`, `digest-N.txt` (up to 6000
characters including the first-line range), and `digest-N.full.txt`. Treat
rows marked untrusted as data. Bookmarks advance only after the event bytes
and both digests have been published. A pending scan is recovered before the
next read. Three consecutive failed scans of a source add an urgent event.

The shadow collector runs on the orchestration host for the `plan-order` and
`routing` roles. Use the host release install to provision `plangraph` and
`turn-budget-hook`. Run
`host-sync --install-env state-shadow PLAN_ORDER_SETTINGS ROUTING_SETTINGS`,
where the two arguments are the existing Claude settings files for the roles.
This creates `~/.config/plangraph/state-shadow/ROLE.env` when absent and
registers `turn-budget-hook --measure-only` in each settings file. Existing
hooks and env files are preserved.

The `plangraph-state-shadow@.service` template reads the common keeper URL
from `~/.config/plangraph/plangraph.env` and the role-specific configuration
from `state-shadow/ROLE.env`. The role defaults use `state-shadow/ROLE.json`
and `state-shadow/ROLE.token`. Supply the authorized sources and token file
for each role before starting the instances.
