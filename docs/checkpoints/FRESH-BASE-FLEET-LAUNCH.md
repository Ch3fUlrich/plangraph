# FRESH-BASE-FLEET-LAUNCH: CLI slice

`plangraph base-fresh <repo>` fetches the origin default branch and checks the
selected checkout's HEAD using `plangraph.spawner.base_fresh.base_fresh`, the
existing admission gate. There is no second Git or ancestry implementation.
The Rust command invokes the installed Python package; `PG_SPAWND_PYTHON`
selects the interpreter, matching the spawner adapter. Otherwise it uses
`python3` on POSIX or `python` on Windows. It does not load setup's environment
file and does not create sessions, branches, or worktrees.

Exit 0 prints `base_current` on stdout. Exit 1 prints the shared gate's
`base_stale: ...` reason on stderr. If the interpreter/package cannot run or
returns an invalid result, it prints `base_stale: gate_unavailable`. Backend
diagnostics are captured so each result contains exactly one reason line.

The gate checks HEAD, so a launcher must call it on the checkout it will use
to cut its session tree, before writing launch state or creating a worktree,
and propagate a nonzero status without continuing. Fetch updates Git's remote
tracking metadata; success does not fast-forward HEAD.

Open: wire the external routing `bin/fleet-launch` entry point and test its
actual stale refusal and current launch path. That source is absent from this
plangraph checkout. The CLI fixture tests prove command behavior, not launcher
integration. Session launch and external package-worktree coverage remain as
documented in `docs/SPAWNER.md`.
