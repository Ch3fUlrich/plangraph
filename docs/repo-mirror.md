# Repository mirrors (REPO-MIRROR slice 1)

`pg-ctx::mirror::Mirror::from_repos(config, root)` reads `repos.json` as a JSON
object, e.g. `{"demo":"https://example.invalid/demo.git"}`. Names start with an
ASCII letter/digit and contain only letters/digits, `.`, `_`, `-`; use absolute
paths for local remotes. Each repository gets `root/<name>/mirror.git` (bare,
never a worktree), `fetch.lock`, and an atomically replaced `watermark.json`.

`fetch_named(name, &["README.md", "docs/guide.md"])` names the files whose blob
SHAs must be recorded. It clones with `git clone --bare` initially, then fetches
only main with `--prune` into a candidate ref in a private staging copy. It checks
ancestry and resolves each named path at that exact commit before updating accepted main or
`{repo, main_head, blob_shas, fetched_at}` (Unix seconds). `blob_shas` maps each
repository-relative path to its Git blob object ID; missing files, directories
and invalid paths refuse the update with `Alarm::FetchFailed`. `fetch(name)`
refreshes the previously recorded named set (empty on first use);
`fetch_named` replaces that set, and an empty slice explicitly clears it.
Older watermarks without `blob_shas` deserialize with an empty map.
Rewinds/divergence return
`Alarm::ForcePush`; Git, storage and lock errors return `Alarm::FetchFailed`.
Both leave the prior watermark unchanged. Rejected refs and objects are discarded
with staging, never published in the accepted mirror; successful publication also
removes the candidate ref. A per-repo lock waits for overlapping writers;
timestamps cannot decrease even if the clock rolls back. `watermark(name)` reads
the last accepted state; `age()` saturates at zero and `is_stale()` is true only
after 300 seconds. Unchanged successful fetches refresh freshness.

If `watermark.json` is missing, the mirror's own `refs/heads/main` supplies the
ancestry baseline before fetching. Rewinds and divergence still return
`Alarm::ForcePush`, leaving accepted main untouched and the watermark absent;
a missing or unreadable main ref returns `Alarm::FetchFailed` rather than
accepting an unverified update. A fast-forward recreates the watermark.
If a watermark exists but its baseline commit is absent locally, fetching may
restore it through main's history. If it remains absent, return `Alarm::ForcePush`:
the upstream history cannot prove a fast-forward. Preserve the watermark and
keep every fetch in staging until all ancestry/blob checks pass. Restoring
upstream history permits the next fetch without deleting the watermark.
All mirror Git commands ignore system/global Git configuration (including URL
rewrites and protocol overrides) in production as well as tests; inherited
`GIT_*` variables are cleared by the shared command builder. Subprocess tests
inject `GIT_DIR`, `GIT_COMMON_DIR`, worktree/object/index overrides, and indexed
configuration (`GIT_CONFIG_COUNT`/`KEY_0`/`VALUE_0`) as well as HOME/XDG config.
The parent requires a completion file written after both fetches, so an exact
test filter matching zero tests cannot silently pass. Direct child-test runs
without the fixture execute the full isolated parent test instead of doing nothing.

Run `cd rust && cargo test -p pg-ctx` for local-only bare-origin tests.
Slice 1 is this one crate's bare fetch and watermark (main head plus named blob
SHAs). Keeper wiring, scheduling/timers and other repositories are later slices.

## Blocking slice-1 requirement

The no-Git-subprocess requirement is **not met** by the current backend. The
evaluated `gix` 0.90.0 uses `gix-transport` 0.62.0's
`client::blocking_io::file::connect` for local clone/fetch; that function explicitly
spawns `git-upload-pack` (with a `git upload-pack` fallback). Its fetch API also
does not implement pruning (`Remote::prune` is commented out). A subprocess-free
local clone/fetch equivalent is unavailable in that version, so the backend swap
is blocked, not deferred to a sanctioned later slice. No unused dependency or
partial backend claiming to satisfy that requirement is added. The existing Git
backend remains explicit here until that blocker is resolved.

The pre-existing lane also includes keeper ready-file startup work in
`daemon.py`, the contract harness, its startup regression, `KEEPER.md`, and two
changelog bullets. That scope finding remains unresolved: removing the startup
implementation would break the existing startup regression, while removing or
weakening that test is forbidden in this round. No keeper mirror wiring is added.
The reported `ready` signature mismatch does not apply: `daemon.run` already
accepts `ready` on main. The documentation now attributes startup to `daemon.py`.
