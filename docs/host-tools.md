# Rust host tools

Build host binaries with `cargo build -p pg-worker-core --bins --release` in
`rust/`. Install binaries in `~/.local/bin` on each worker host.

`pg-fleet-series sample --host ID` appends one minute sample under
`~/.local/state/plangraph/fleet/ID.ndjson`. `probe` emits without persisting;
`--ssh DEST` collects a remote worker host using the same installed binary.
Failures persist `reachable=false`; unavailable measurements are null.
The state root can be overridden with `--state DIR`.
Running/queued records are read from `DIR/runs/*/record.json`; session IDs
come from `session_id` or `session`. This measures recorded agent sessions.
Only adjacent reachable samples with an increased OOM counter mark sessions lost.
`pg-fleet-series summary --hours 24` prints mean/p90 runs, idle-with-ready hours,
OOM increments, lost sessions and unreachable minutes, including missing minutes.
Rust run records gain `oom_kill_delta` at terminal transition, using their stored
host and sampled interval; missing/recently unavailable samples produce null.
The Rust command replaces the proposed Python fleet-series entry point.

Install the fleet service and timer in `~/.config/systemd/user/`, and create
`~/.config/plangraph/fleet-worker.env` with `PLANGRAPH_HOST_NAME` set to the
registry ID. Enable with `systemctl --user enable --now pg-fleet-series@worker.timer`.
Deployment and multi-host evidence require operator access and are not local tests.

Before each package start, call `pg-package-host` with the existing
`~/.config/plangraph/hosts.json` registry (`--registry` overrides it).
It uses samples at most 120 seconds old and probes stale/missing hosts over SSH.
It reserves 2048 MB above a 3000 MB RAM floor, gates at load/core >= 1,
then orders by load/core, swap, runs, recorded sessions, free RAM and registry order.
Windows workstation placement requires `--allow-workstation`.
JSON includes the selected host, measurements and every admission refusal.
Exit 5 means queue: no host admits. `--reservation-mb` sets package memory.
This admission module is also the Rust overflow spawner's policy source.
The phase-batch-orchestration skill is absent from this checkout; its caller
must invoke this command before launch when that skill is available.

Before launching Codex, run `pg-host-readiness --project PATH --sandbox workspace-write`.
It checks version, login, native sandbox startup, and a commit in an independent
temporary repository's worktree, with a 30 second command timeout and no model call.
`--scratch-root` chooses a filesystem matching the intended workspace.
`--codex` selects the executable. It reads `CODEX_HOME/config.toml` (or `HOME/.codex`),
requires folder trust, disabled startup updates and at least 3000 MB free RAM.
It prints `READY` only with no missing checks, otherwise `MISSING` lines and exit 1.
Configured hooks/plugins currently report unverified trust: persisted hook hashes
are CLI-owned and this tool does not assume their storage schema. This is a known
limitation for hosts using hooks; it never accepts an unproven hook trust state.
The phase-batch skill must call the check on the chosen host before a package.
See the official [configuration reference](https://developers.openai.com/codex/config-reference)
and [sandbox command reference](https://developers.openai.com/codex/cli/reference).

`pg-repo-sync --repo PATH --check-config --host ID --allow-name NAME --allow-email EMAIL`
checks a main checkout's local `core.bare` and identity overrides without writing.
Repeat allow-list flags for accepted host identities. Absent local overrides inherit
the host identity; explicit overrides must match. Drift prints one `ALERT` line with
host, path and escaped values and exits 1. Clean checkouts are silent.
Git context variables, including `GIT_DIR` and injected config, are removed from
child processes so fixtures and host checks cannot address unrelated metadata.
