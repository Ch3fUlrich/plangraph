# Repository mirrors (REPO-MIRROR slice 1)

`pg-ctx::mirror::Mirror::from_repos(config, root)` reads `repos.json` as a JSON
object, e.g. `{"demo":"https://example.invalid/demo.git"}`. Names start with an
ASCII letter/digit and contain only letters/digits, `.`, `_`, `-`; use absolute
paths for local remotes. Each repository gets `root/<name>/mirror.git` (bare,
never a worktree), `fetch.lock`, and an atomically replaced `watermark.json`.

`fetch_named(name, &["README.md", "docs/guide.md"])` names the files whose blob
SHAs must be recorded. It clones with `git clone --bare` initially, then fetches
only main with `--prune` into a candidate ref. It checks ancestry and resolves
each named path at that exact commit before updating accepted main or
`{repo, main_head, blob_shas, fetched_at}` (Unix seconds). `blob_shas` maps each
repository-relative path to its Git blob object ID; missing files, directories
and invalid paths refuse the update with `Alarm::FetchFailed`. `fetch(name)`
refreshes the previously recorded named set (empty on first use);
`fetch_named` replaces that set, and an empty slice explicitly clears it.
Older watermarks without `blob_shas` deserialize with an empty map.
Rewinds/divergence return
`Alarm::ForcePush`; Git, storage and lock errors return `Alarm::FetchFailed`.
Both leave the prior watermark unchanged. A per-repo lock serializes writers;
timestamps cannot decrease even if the clock rolls back. `watermark(name)` reads
the last accepted state; `age()` saturates at zero and `is_stale()` is true at
300 seconds. Unchanged successful fetches refresh freshness.

If `watermark.json` is missing, the mirror's own `refs/heads/main` supplies the
ancestry baseline before fetching. Rewinds and divergence still return
`Alarm::ForcePush`, leaving accepted main untouched and the watermark absent;
a missing or unreadable main ref returns `Alarm::FetchFailed` rather than
accepting an unverified update. A fast-forward recreates the watermark.
If a watermark exists but its baseline commit is absent locally, fetching may
restore it through main's history. If it remains absent, return `Alarm::ForcePush`:
the upstream history cannot prove a fast-forward. Preserve the watermark and
keep a rebuilt clone in staging until all ancestry/blob checks pass. Restoring
upstream history permits the next fetch without deleting the watermark.
All mirror Git commands ignore system/global Git configuration (including URL
rewrites and protocol overrides) in production as well as tests; inherited
`GIT_*` variables are cleared by the shared command builder. Subprocess tests
inject `GIT_DIR`, `GIT_COMMON_DIR`, worktree/object/index overrides, and indexed
configuration (`GIT_CONFIG_COUNT`/`KEY_0`/`VALUE_0`) as well as HOME/XDG config.

Run `cd rust && cargo test -p pg-ctx` for local-only bare-origin tests.
Slice 1 is this one crate's bare fetch and watermark (main head plus named blob
SHAs). Keeper wiring, scheduling/timers, other repositories, HTTP/blob-content
APIs, alarm delivery and a gix backend are deferred.
