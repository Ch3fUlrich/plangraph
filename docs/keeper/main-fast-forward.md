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

An inconclusive unit query holds and names its cause: bus unreachable, timed out,
query failed, or unreadable unit state. A missing/unreadable `ps`/CIM process-query tool or
a failed process query also holds with a fixed diagnostic. Tool stderr, command
lines and config values are not copied into logs. A live unit/process without
matching state holds with a bootstrap/path diagnostic. After deployment, restart
the keeper once, then verify the updater's state path is the configured DB filename
plus `.loaded.json` before enabling the timer. This covers older running keepers
and accidentally pointing the updater at a different DB. Custom unit names must
set the same `--keeper-unit` in the template. An unreachable user bus plus a
not-found system unit and no detected process still holds: the user unit is
unknown, not proven absent. Do not treat a failed process scan as an empty scan.
Repair the bus/session or query-tool installation and rerun; this remaining
operator-recoverable hold is deliberate, including on headless cron hosts.

The startup flock is still not shared with the daemon. Keeper modules are loaded
at daemon module import time, before `main()`/`run()` or state publication. A lock
around `write_loaded_state()` would falsely imply those imports were protected.
Protecting them requires a pre-import bootstrap/launcher for CLI and embedded
entry points; that is deferred from this narrow review round. The immediate
pre-merge recheck remains, with a residual race after that check.

Loaded state includes `loaded_branch` (null for detached HEAD) and `dirty`.
A clean detached checkout has an exact SHA and can be compared normally. Tracked
local changes make `loaded_sha` unknown, so updates hold until a clean restart;
untracked files are not a code identity. A non-Git installation records unknown
SHA/branch/dirt and also holds indefinitely until restarted from identifiable Git
code (or the keeper is stopped); no arbitrary HEAD is asserted for those installs.

Runtime data trace outside the keeper package:

- `api_routes._phase_plan` -> planner service/routing ->
  `routing_support.catalog_db.CatalogDB.init_and_seed` reads
  `src/plangraph/routing_support/catalog_schema.sql`.
- The same routing path calls `planner.route_policy.load_route_policy`, reading
  `src/plangraph/planner/route_policy.json`.
- Routing admission's resolver host probes call `opencode_overlay.overlay_env` /
  `_load_root_providers`, reading `configuration/opencode/providers.json`.

These three exact existing paths are watched in addition to Python imports and
keeper-package files. The old spawner catalog schema is not read by this path;
review `tail*.md` belongs to seat prompt construction, and config/empty-mcp.json
is passed to a child CLI. The prompt-transport contract loader is not called by
keeper routing/probes. They are not blanket-watched. DBs, snapshots, tokens,
keeper.json and operator-selected settings/override files are deployment data,
not fixed data from this code tree; changing those remains operator configuration.
