# Fleet code sync

Run `python3 -m plangraph.fleet_sync release record` after fetching origin/main.
The newest locally known main ancestor with successful required branch checks is
recorded privately and atomically in `~/.local/state/plangraph/release.json`.
The CLI queries `gh api`; missing protection or required checks fails closed.
`release show` prints the record. No checkout is changed.

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

## Advance and rollback (slice 2)

`advance --sha SHA --hosts host-a host-b --go D-NNNN [--dry-run]`
uses the same optional `--registry PATH`. The first host is the canary; hosts
are processed serially and the first failure stops the rollout (exit 1).
SHA must exactly match release.json. D-825 requires a router GO reference for
each SHA; retain that requirement until 10 clean live runs. This command records
the supplied decision reference, not an independent verification of router approval.

Set `repos.plangraph.live_dir` to an absolute `<live_root>/current`, outside
`worktrees_root`; generated consumer configs continue to use that path. Releases
are detached clones at `<live_root>/releases/<sha>`, independent of worktree cleanup.
Configure `health_checks` with three argv arrays: `doctor` (spawner doctor),
`whoami` (keeper whoami), and `spawn` (one dry-run spawn). These commands run from
the candidate checkout before switching and from current after switching. Commands
are injected configuration: operators must ensure spawn includes its dry-run flag.
No shell expansion occurs for health commands; output is suppressed.

POSIX current is atomically replaced as a symlink. Windows uses an atomically
replaced `current.pointer` file containing the absolute release path; Windows
launchers must resolve that pointer (ordinary path consumers need an adapter).
Any prepare/health/switch/prune failure restores the previous target and marks the
host FAILED. Earlier successful hosts are not reverted. Transport loss is FAILED;
inspect the host before retrying because acknowledgement may have been lost.
For an intentional rollback, record the previously approved release and obtain
a fresh GO, then advance to it. Do not hand-edit generated host configs.

Pruning retains three releases, always including current and previous.
`--dry-run` validates inputs and prints each host plan without contacting hosts,
writing logs, or changing files. Actual runs append JSON step/status records to
`~/.local/state/plangraph/fleet-sync.log`, excluding commands, env and error text.
Run only one advance per fleet at a time. Routerctl integration is a later slice.
