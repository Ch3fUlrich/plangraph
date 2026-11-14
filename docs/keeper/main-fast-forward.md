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
