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

An absent/stale state file now also checks the existing user unit's `MainPID`
(`plangraph-keeper.service`, override with `--keeper-unit`). A live unit with no
matching state holds with a bootstrap/path diagnostic. A failed unit query also
holds; only a confirmed zero PID permits an absent state. After deploying this
writer, restart the keeper once, then verify the updater's state path is the
configured DB filename plus `.loaded.json` before enabling the timer. This covers
older running keepers and accidentally pointing the updater at a different DB.
Units with a custom name must configure the same `--keeper-unit` in the template.

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
