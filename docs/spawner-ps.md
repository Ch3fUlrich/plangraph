# Cross-host ps

`python3 -m plangraph.spawner.cli ps --all-hosts` uses the same merged listing
as MCP `plangraph_agent_ps` (plain `ps` also includes configured hosts).
Without site configuration, ps retains its local-only behavior. An optional
`$XDG_CONFIG_HOME/plangraph/ps-hosts.json` (default: `~/.config/plangraph/ps-hosts.json`)
supplies `{"hosts": [{"name": "display-label", "target": "ssh-alias"}]}`.
Keep this file private and outside the repository. Names are public display
labels; targets and transport stderr never appear in replies.
Each POSIX host needs `python3` and plangraph available to its SSH shell.
Windows entries use `win_src`, for example `C:/source space/plangraph/src`;
the command normalizes path separators for cmd.exe, groups the source guard
so an existing directory reaches Python, and runs
`python -m plangraph.spawner.remote_ps` with that PYTHONPATH.
A host's checkout path and optional `state_dir` come from the host registry when it has one
(`hosts.json`, matched by id, alias or SSH target); the query runs with `PLANGRAPH_SPAWN_STATE` set to
`state_dir`, so runs launched into a nondefault state directory are listed.
Queries run concurrently with a three-second subprocess timeout (nine seconds for a WSL or Windows host) and no interactive
authentication or retries. The remote endpoint reads only local records, preventing
recursive fan-out. Only live remote records are included, labeled with the
configured host name. Failures yield a named `unknown` row with the fixed reason
`host query failed`, `host src missing` (exit 44), or `host unreachable` (exit 255).
Transport failures additionally expose a bounded `error` field containing only a
recognized exception class and SSH exit code, such as `HostQueryError (SSH exit 1)`.
Exception messages and stderr remain private, including missing-module diagnostics;
stderr size cannot invalidate a successful response. MCP preserves the structured
error and never truncates unknown rows as ended-run history.
Doctor checks each entry with `src` or `win_src` in parallel, reporting `ok`,
`src missing`, or `unreachable`, with a three-second timeout per host.
An empty XDG setting uses the default config location. Invalid entries produce
an `invalid-config` unknown row without suppressing valid entries. An unreadable,
malformed, or incorrectly shaped configuration file produces one `invalid-config`
unknown row with reason `host query failed`, preserving the local listing without
exposing configuration contents. A missing file or an empty hosts list remains
local-only. Fan-out uses at most 16 workers; stdout is capped at 1 MiB and stderr
is discarded. MCP performs the fan-out off the event loop. Only unknown rows
expose reasons.
