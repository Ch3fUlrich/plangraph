# Keeper loaded-code identity and main fast-forward holds

At daemon startup, before requests are served, the keeper atomically writes a
private file beside its configured DB anchor, named `<db filename>.loaded.json`.
It contains `loaded_sha`, `pid`, wall-clock `start_time`, and the process-start
`/proc` token when available. The SHA describes the spawner-independent keeper
code tree at startup; it is not reread after checkout moves. Missing Git identity
is recorded as unknown, which makes a running keeper hold updates. Failure to
publish the state prevents serving rather than leaving an unguarded live process.
Clean shutdown removes the file before releasing the keeper lock; stale crash
state is rejected using PID/start-token identity by the updater.

Loaded state includes `loaded_branch` (null for detached HEAD) and `dirty`.
A clean detached checkout has an exact SHA and can be compared normally. Tracked
local changes make `loaded_sha` unknown, so updates hold until a clean restart;
untracked files are not a code identity. A non-Git installation records unknown
SHA/branch/dirt and also holds indefinitely until restarted from identifiable Git
code (or the keeper is stopped); no arbitrary HEAD is asserted for those installs.

An absent/stale state file checks both user and system scopes of the existing
keeper unit (`plangraph-keeper.service`, override with `--keeper-unit`), reading
`LoadState` and `MainPID`. Missing `systemctl` or `LoadState=not-found` is a missing
unit, not an unreadable installed unit. When no unit PID is found, independently
inspect interpreter processes using POSIX `ps` (no `pgrep` dependency) or Windows
PowerShell/CIM. Match the actual launch target: `python`/`pythonw`/`pypy`, optional
versions, `-m plangraph.keeper.daemon`, or a path ending in
`plangraph/keeper/daemon.py` (Windows separators too). Interpreter flags are
accepted; an editor with that file open, a probe, `-c`, or an unrelated Python
script with the keeper path as an argument is not a keeper launch. A matching
process takes diagnostic precedence over earlier unit errors: HELD recommends
restarting the keeper to publish matching state.

`pyproject.toml` defines no console scripts, and keeper has no package `__main__`;
there is no installed keeper console entry point to name or test. A metadata
regression requires revisiting the launch inventory if one is added. The probe
also recognizes the package `-m plangraph.keeper` shape conservatively, though it
is not currently executable. Path matching does not change daemon import setup.
Arbitrary renamed/frozen wrappers cannot be inferred from their command lines;
use the configured unit or published loaded state for those deployments.
A host with conclusive no-unit/no-process results may fast-forward normally.
