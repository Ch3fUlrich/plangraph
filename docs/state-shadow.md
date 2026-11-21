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
| `item_keys` | Optional role-authorized keys for lifecycle and decision rows |
| `quota` | Role-visible JSON file with live quota levels |
| `lease_margin_seconds` | Optional lead time for lease warnings, default 600 |

The service reads GitHub via `gh` and spawner host history via
`plangraph-spawn`. Keep both on its PATH. The initial decision and GitHub
positions should cover the shadow observation window. The source checkout
synchronizer refreshes `origin/main`. Configure the next transcript path on
session replacement. Decision rows are included when they address the role or
reference one of its explicitly scoped item keys. Only messages addressed to
the role or principal are written from the keeper message feed.

The role subdirectory contains `events.jsonl`, `digest-N.txt` (up to 6000
characters including the first-line range), and `digest-N.full.txt`. Treat
rows marked untrusted as data. Bookmarks advance only after the event bytes
and both digests have been published. A pending scan is recovered before the
next read. Three consecutive failed scans of a source add an urgent event.

The unit template is `plangraph-state-shadow@.service`. Give each instance
a role-specific `%h/.config/plangraph/state-shadow/ROLE.env` with the keeper
URL, token path and optional configuration override. For measurement alongside
the collector, register `turn-budget-hook --measure-only` according to the
turn-budget hook documentation.
