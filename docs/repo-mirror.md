# Repository mirrors (REPO-MIRROR slice 1)

`pg-ctx::mirror::Mirror::from_repos(config, root)` reads `repos.json` as a JSON
object, e.g. `{"demo":"https://example.invalid/demo.git"}`. Names start with an
ASCII letter/digit and contain only letters/digits, `.`, `_`, `-`; use absolute
paths for local remotes. Each repository gets `root/<name>/mirror.git` (bare,
never a worktree), `fetch.lock`, and an atomically replaced `watermark.json`.

`fetch(name)` clones with `git clone --bare` initially, then fetches only main
with `--prune` into a candidate ref. It checks ancestry before updating accepted
main or `{repo, main_head, fetched_at}` (Unix seconds). Rewinds/divergence return
`Alarm::ForcePush`; Git, storage and lock errors return `Alarm::FetchFailed`.
Both leave the prior watermark unchanged. A per-repo lock serializes writers;
timestamps cannot decrease even if the clock rolls back. `watermark(name)` reads
the last accepted state; `age()` saturates at zero and `is_stale()` is true at
300 seconds. Unchanged successful fetches refresh freshness.

Run `cd rust && cargo test -p pg-ctx mirror` for local-only bare-origin tests.
Keeper scheduling, HTTP/blob APIs, alarm delivery and a gix backend are deferred.
The shared `pg-ctx` crate also exposes the independent [capability gate](dup-gates.md); mirror APIs are unchanged.
