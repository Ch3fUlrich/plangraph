# Spawner doctor host source checks


`python3 -m plangraph.spawner.doctor` delegates host source checks to the Rust
`pg-spawnd doctor-sources` subcommand. `doctor.py` resolves it on the host's
`PATH`, never from checkout build artifacts, and reads its JSON check rows with
a derived subprocess timeout: ten seconds plus three seconds per batch of up to
16 configured hosts (at least ten seconds). Before any checks, optional `env.PATH` from the
local hosts registry entry replaces the inherited PATH for both pg-spawnd and
harness discovery/children. Identity is `PLANGRAPH_HOST_NAME` or registry `self`,
with case-insensitive id/alias lookup; absent configuration retains inherited PATH.
Windows PATH keys are case-insensitive (`Path` is accepted).
A present but unusable registry produces `host_path: fail`, distinguishing an identity mismatch from invalid
configuration without printing registry or environment values. PATH is restored after checks, even on exceptions.
There is no Python fallback for source checks; fresh release clones need no build.
Fleet snapshots likewise delegate the read-only JSON projection to
`pg-spawnd fleet-snapshot`; the Python loader supplies persisted registry and run
records without probing, and projection failures preserve the existing
`fleet: null` fallback.

The Rust command reads `plangraph/ps-hosts.json` under `XDG_CONFIG_HOME` (or
`~/.config`), checking valid entries with `src` or `win_src` in parallel with
at most 16 workers. SSH uses batch mode, a two-second connection timeout and
a three-second probe deadline; source checks support POSIX, WSL and Windows
paths. SSH output and diagnostics are discarded. A missing config or no
eligible entries produces no source check rows; unreadable or malformed
configuration produces `ps_hosts: fail` with `invalid configuration`.

Each `ps_host_<name>` row passes with `ok` on SSH exit 0; exit 44 reports
`src missing`, exit 255 or an SSH launch/I/O error reports `unreachable`, and
a timeout or other exit reports `host query failed`. If the Rust binary is
missing, Python adds `ps_hosts: fail` with `unavailable`; a nonzero subcommand
exit, invalid JSON/non-list response, timeout or other invocation error instead
reports `host query failed`. Any failed check makes doctor exit 1.
