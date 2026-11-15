# Cross-host ps

`python3 -m plangraph.spawner.cli ps --all-hosts` uses the same merged listing
as MCP `plangraph_agent_ps` (plain `ps` also includes configured hosts).
Without site configuration, ps retains its local-only behavior. An optional
`$XDG_CONFIG_HOME/plangraph/ps-hosts.json` (default: `~/.config/plangraph/ps-hosts.json`)
supplies `{"hosts": [{"name": "display-label", "target": "ssh-alias"}]}`.
Keep this file private and outside the repository. Names are public display
labels; targets are SSH destinations and are redacted from diagnostics.
Each POSIX host needs `python3` and plangraph available to its SSH shell.
Windows entries use `win_src`, for example `C:/source space/plangraph/src`;
the command normalizes path separators for cmd.exe, groups the source guard
so an existing directory reaches Python, and runs
`python -m plangraph.spawner.remote_ps` with that PYTHONPATH.
Queries run concurrently with a three-second subprocess timeout per attempt and
no interactive authentication. SSH exit 255 (including connect timeout) gets one
retry; remote command errors and whole-query timeouts do not retry.
The remote endpoint reads only local records, preventing recursive fan-out.
Only live remote records are included, labeled with the configured host name.
Failures yield a named `unknown` row; transport failures include the underlying
error class and first stderr line, redacted before truncation to 200 characters.
Missing configured source directories use `host src missing`; SSH exit 255 uses
`host unreachable`, followed by the error class and safe stderr when available.
Malformed output uses `host query failed` followed by the error class. MCP preserves these diagnostics and
never truncates unknown rows as ended-run history.
Doctor checks each entry with `src` or `win_src` in parallel, reporting `ok`,
`src missing`, or `unreachable`, with a three-second timeout per host.
An empty XDG setting uses the default config location. Invalid entries produce
an `invalid-config` unknown row without suppressing valid entries. An unreadable,
malformed, or incorrectly shaped configuration file produces one `invalid-config`
unknown row with reason `host query failed`, preserving the local listing without
exposing configuration contents. A missing file or an empty hosts list remains
local-only. Fan-out uses at most 16 workers; stdout and stderr are each capped at
1 MiB. MCP performs the fan-out off the event loop. Only unknown rows expose reasons.
