# Fleet code sync: slice 1

Run `python3 -m plangraph.fleet_sync release record` after fetching origin/main.
The newest of the last 50 locally known main commits with successful required branch checks is
recorded privately and atomically in `~/.local/state/plangraph/release.json`.
The CLI queries `gh api`; missing protection or required checks fails closed.
`release show` prints the record. No checkout is changed.
The Python `record(max_commits=50)` API can configure the scan window; scanning
stops at the first green commit and fails closed if none is found in the window.
Check results are selected by completed/started (or status updated/created)
timestamp, not API response order; equal-time conflicting results fail closed.

`genconfig write --registry PATH` derives ps-hosts.json and overflow-hosts.json
under `$XDG_CONFIG_HOME/plangraph` (default `~/.config/plangraph`). Each file is
atomically replaced; the pair is not a transaction. `genconfig check` exits 1
on semantic drift, including hand edits. Both print a one-line summary.
The default registry is that directory's hosts.json. SSH ids must resolve through
SSH configuration. Set optional `repos.plangraph.live_dir` to the pinned live
checkout; no fallback to a developer checkout is allowed. Local entries are
excluded from remote configs; keeper entries fail closed. Windows SSH hosts
are ps-only. Overflow retains a deny-by-default empty public_repos allowlist.

`drift --registry PATH` prints host, live SHA, release SHA, commits behind.
`drift --json` emits an array. Missing objects or unreachable transports produce
`unknown`, never a fabricated zero; drift always exits 0. Counts use local Git
objects, so fetch the release and host commits first.

Later slices add advance, canary health checks, automatic rollback and routerctl
integration. This slice never invokes deployment, keeper or model endpoints.
