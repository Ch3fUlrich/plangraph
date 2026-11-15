# Keeper loaded-code identity and main fast-forward holds

At daemon startup, before requests are served, the keeper atomically writes a
private file beside its configured DB anchor, named `<db filename>.loaded.json`.
It contains `loaded_sha`, `pid`, wall-clock `start_time`, and the process-start
`/proc` token when available. The SHA describes the spawner-independent keeper
code tree sampled before package imports and verified after publication.
A changed identity during startup records unknown; it is not refreshed while serving. Missing Git identity
is recorded as unknown, which makes a running keeper hold updates. Failure to
publish the state refuses serving with a named error and nonzero exit, without a traceback.
Clean shutdown removes the file before releasing the keeper lock; stale crash
state is rejected using PID/start-token identity by the updater.

The fleet-sync `main-ff` command compares loaded code against origin/main before
moving. Its static closure follows tracked Python modules rooted in the existing
keeper package, including relative imports, imports inside functions and literal
`__import__` / `import_module` calls. It reads Git blobs at both revisions, not
whatever files happen to be in the working tree. Existing non-Python files under
the keeper directory are guarded too. This is a static repository import check;
operator-provided external plugins or computed nonliteral imports outside the
keeper package are not inferred from runtime state.

An imported module outside the keeper package still holds on modification or
deletion. A new unused file alone does not hold. A clean main with only unrelated
spawner changes may advance while the keeper retains its original loaded SHA.
A hold prints and caches `HELD keeper code changed: ... loaded ... target ...`,
exits 3 and leaves HEAD unchanged. Existing fleet-sync drift output shows the
cached status for local hosts. HTTP health stays free of filesystem I/O.

Deploy the keeper state writer and restart from a known main revision before
activating the provided updater template. This package does not restart a keeper,
install a timer, change live state, or edit the external routerctl implementation.

The updater rechecks loaded state and the changed-file closure immediately before
`merge --ff-only`, including a keeper restart during its initial check. Updates
must use this guarded command: an independent manual `git pull` does not take its
lock or run its checks. It cannot serialize an arbitrary Git writer.

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

The revived probe is implemented by `pg-mainff` in `rust/pg-mainff`.
`cargo run --manifest-path rust/Cargo.toml -p pg-mainff -- probe [UNIT]`
prints the detected keeper PID as JSON (zero when conclusively absent);
`running STATE` prints the loaded SHA or null. Query failures return a fixed
public diagnostic with exit code 2. Windows liveness never signals a process.

The Rust `status CHECKOUT` command reads the cached line from that checkout's Git
directory and prints JSON (null if absent/unreadable). The Python drift command
uses this same status reader for local hosts. The template includes the standard
user Cargo bin directory in PATH; adapt it if Rust is installed elsewhere.
