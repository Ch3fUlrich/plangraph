# Cross-host ps

Without site configuration, ps retains its local-only behavior. An optional
`$XDG_CONFIG_HOME/plangraph/ps-hosts.json` (default: `~/.config/plangraph/ps-hosts.json`)
supplies `{"hosts": [{"name": "display-label", "target": "ssh-alias"}]}`.
Keep this file private and outside the repository. Names are public display
labels, not credentials; targets are SSH destinations and never appear in replies.
Each host needs Python and plangraph available to its noninteractive SSH shell.
Queries run concurrently with a three-second subprocess timeout and no interactive
authentication. The remote endpoint reads only local records, preventing recursive
fan-out. Only live remote records are included, with their original host identity.
Failures, including malformed output, yield a named `unknown` row with the fixed
reason `host query failed`; MCP never truncates these rows as ended-run history.
An empty XDG setting uses the default config location. Invalid entries produce
an `invalid-config` unknown row without suppressing valid entries. Fan-out uses
at most 16 workers; each response is capped at 1 MiB. MCP performs the fan-out
off the event loop. Only unknown rows expose the fixed failure reason.
