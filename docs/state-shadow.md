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
| `transcript` | Path to the role's active Claude JSONL transcript (written by the launcher) |
| `session_id` | Session UUID, matching the transcript filename stem (written by the launcher) |
| `data_repo` | Local data checkout with a refreshed `origin/main` |
| `decision_base` | Initial full commit ID on the history of data `origin/main` |
| `github_repo` | Authorized repository in owner/repo format |
| `github_since` | Initial lower bound for main runs, as an RFC 3339 instant |
| `pull_requests` | Optional list of authorized held or train PR numbers |
| `item_keys` | Optional role-authorized keys for decision rows |
| `quota` | JSON file the collector refreshes each scan with the router's booked leg limits (`limited_until`) and the remaining percent per provider; older than 10 minutes fails the source |
| `lease_margin_seconds` | Optional lead time for lease warnings, default 600 |

The service reads GitHub via `gh` and spawner host history via
`plangraph-spawn`. Keep both on its PATH. Enable the Rust lifecycle publisher in
legacy spawner and watchdog environments before starting shadow observation.
Set `PLANGRAPH_LIFECYCLE_PUBLISHER` to the installed `pg-spawnd` binary. The initial decision and GitHub
positions should cover the shadow observation window. The source checkout
synchronizer refreshes `origin/main`. Decision rows are included when they address the role or
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
`host-sync --install-env state-shadow` (it takes no settings files).
This creates `~/.config/plangraph/state-shadow/ROLE.env` when absent and keeps
existing env files. It registers no hook: a profile shared with other roles
would leak `--measure-only` to them, so the launcher writes the hook into each
role's own settings file.

The `plangraph-state-shadow@.service` template reads the common keeper URL
from `~/.config/plangraph/plangraph.env` and the role-specific configuration
from `state-shadow/ROLE.env`. The role defaults use `state-shadow/ROLE.json`
and `state-shadow/ROLE.token`. Supply the authorized sources and token file
for each role before starting the instances.

## Following session recycles

The role's session changes at every handoff and its worktree, hence its
transcript project directory, can move. Three parts keep the collector on the
live session:

- Every `fleet-launch` of a role with `state-shadow/ROLE.env` rewrites the role
  configuration (the last `PLANGRAPH_STATE_CONFIG` of that env file, in the service's syntax: quoted or not, `\ ` escapes, spaces around `=`; else
  `state-shadow/ROLE.json`) with the new `session_id` and the transcript found
  under any `~/.claude/projects/*` directory (else the one derived from the
  launch directory). Other fields stay as the operator set them.
- The same launch writes the role's own settings file
  `launch-settings/role-ROLE.json`: the lean profile, the caps and the
  measure-only hook. Roles with equal caps never share it, and the hook is
  present again after every relaunch.
- The collector takes `session_id` and `transcript` at each scan from the
  role's committed launch record (`launch/ROLE.json` in the routing state
  directory), so a replacement the watchdog adopts later is followed too; else
  from the configuration file, looking a missing transcript up by session id.
  Authorization fields are read once.
- Each scan it also refreshes the `quota` file from the router's own states:
  the provider limits routing treats as active (read through routing's scoped
  `LegStateStore.leg_status`, so a successful probe clears a limit here as it
  does there and a busy refusal stays out; AGY and Codex legs, `limited_until` per leg and scope) and the
  quota snapshots' remaining percent. A booked limit or its recovery is a quota
  event. A source that cannot be read (a missing or empty leg-state store, unreadable snapshots) is `null`; when neither can be read the
  file is not rewritten, so it goes stale and the source reports it.
- When the launch record names a session the role file does not (the watchdog
  adopted a replacement later), the collector writes both `session_id` and
  `transcript` into the role file too.

`host-sync --install-env state-shadow` also creates a missing `ROLE.json` from
`state-shadow/ROLE.seed.json` (principal, inbox, data_repo, decision_base,
github_repo, github_since and the optional lists) and the role's launch record;
without both it prints `NEEDS ...` and writes nothing.
