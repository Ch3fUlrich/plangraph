# Spawner run tree

Moved out of [SPAWNER.md](../SPAWNER.md) to keep it under its line cap.

## SHA-named run-tree primitive (SPAWNER-RUN-TREE-S1)

`git_guard.run_tree(state, repo)` attempts to fetch `origin/main`, selects its verified SHA, and uses the detached tree at `<state>/trees/<sha>`.
If fetching fails or times out, it uses the verified tracking ref, not cache directory mtimes.
It creates missing trees, including stale Git registrations even if locked (double `--force`).
Under the SHA's creation lock, the shared ancestry checker checks the selected base against fetched `origin/main` without another fetch, refusing `base_stale` if a parallel admission advanced the ref while selection waited.
An admission that already fetched this repository can use `run_tree(state, repo, fetch=False)`. This check leaves local `main` and live cached trees untouched.

POSIX reaping keeps the selected tree, trees named by nonterminal run records
or records with survivors, trees with held creation locks or context leases, directories
modified within seven days, and dirty trees. A missing/invalid `tree_sha` on a
nonterminal or survivor record, or corrupt/unreadable records, disables reaping
for that call. Only older, clean, unpinned trees with available locks and leases
are removed. Windows selection does no reaping.
With a checkout or `PLANGRAPH_SPAWNER_REPO`, live Python CLI/MCP launches import a supervisor from the selected tree and stamp its SHA before persistence. Installed packages, custom transports, dry runs and direct supervision retain process-load identity. Job cwd and empty seat envelopes retain their existing behavior. The tree lease is released once when the supervisor finishes or the caller detaches; durable running records keep the tree pinned. Cancellation before the admission reply may still stop the pending supervisor. CLI `--wait` bounds the wait after the existing admission handshake. For supervised MCP jobs, use `run_id` for status, result and cancellation; `job_id` controls and steering remain local to the runner that owns the job. The launch-spec module adapter imports launch construction from the selected tree and appends a `run_tree` event, including when child events are null.
Terminal means `runner.is_terminal`; `needs_input` and `orphaned` keep their trees.
