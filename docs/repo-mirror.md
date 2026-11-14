# Repository mirrors (REPO-MIRROR slice 1)

`pg-ctx::mirror::Mirror::from_repos(config, root)` reads `repos.json` as a JSON
object, e.g. `{"demo":"https://example.invalid/demo.git"}`. Names start with an
ASCII letter/digit and contain only letters/digits, `.`, `_`, `-`; use absolute
paths for local remotes. Each repository gets `root/<name>/mirror.git` (bare,
never a worktree), `fetch.lock`, and an atomically replaced `watermark.json`.

`fetch_named(name, &["README.md", "docs/guide.md"])` names the files whose blob
SHAs must be recorded. It initializes a bare repository through `gix`, then fetches
only main into a private quarantine. It checks
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
removes the candidate ref. A per-repo lock waits at most five seconds for overlapping
writers, with capped exponential backoff, then returns `Alarm::FetchFailed`;
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
All mirror operations use isolated `gix` options, ignoring system/global Git
configuration (including URL rewrites and protocol overrides) and inherited
`GIT_*` variables in production as well as tests. Subprocess tests
inject `GIT_DIR`, `GIT_COMMON_DIR`, worktree/object/index overrides, and indexed
configuration (`GIT_CONFIG_COUNT`/`KEY_0`/`VALUE_0`) as well as HOME/XDG config.
The parent removes any prior completion file and requires new evidence written
after both fetches; a zero-test child launch therefore fails the parent. Arbitrary
external test filters can still match zero tests and exit successfully. Direct
child-test runs create their own fixture and exercise fetches without re-running
or spawning the parent. The configured child has an empty executable search path,
proving local mirror operations do not invoke Git or git-upload-pack.

Run `cd rust && cargo test -p pg-ctx` for local-only bare-origin tests.
Slice 1 is this one crate's bare fetch and watermark (main head plus named blob
SHAs). Keeper wiring, scheduling/timers and other repositories are later slices.

## Storage and transport

The single `pg-ctx` implementation uses `gix` for objects, refs and ancestry.
Local paths/file URLs transfer only missing reachable objects in-process, not
gix's helper-spawning file transport. HTTP(S) and Git protocol use native gix
fetch; SSH, external helpers and credential-helper fallback are refused with
`Alarm::FetchFailed`. Only main
is fetched, no tags or other refs are retained, and a deleted main is refused.
No mirror code starts a Git subprocess; tests use Git only to construct/inspect
their disposable local bare remotes.

Quarantines hardlink immutable loose objects and pack/index/bitmap/reverse-index
files on the same filesystem; mutable refs/config/metadata are copied privately.
Symlinks/special files are refused, never dereferenced. Publication exchanges the
accepted directory and quarantine in one atomic Linux rename-exchange syscall;
failure leaves the old mirror in place, interruption after exchange leaves the
new accepted mirror readable, including the prior watermark's objects. There is
no restore rename or accepted-directory deletion window. Unsupported platforms
or filesystems fail closed on replacement. The next locked fetch sweeps only
named `.pg-ctx-fetch-*` quarantines abandoned by an aborted process.
If publication completed before an interrupted watermark write, the next fetch
must fast-forward both accepted main and the older watermark; it cannot silently
rewind the newly accepted mirror back to the watermark's head.

Native HTTP/HTTPS/Git fetch tests replace only the connection opener with pipes
to a disposable local bare remote's test-only upload-pack server. The production
refspec, negotiation and receive path still run, including forced candidate
updates and refusal to invoke a configured credential helper on authentication
failure. No test opens a network connection. Recovery tests read the complete
persisted watermark, retaining named blobs and monotonic freshness when a bare
mirror is rebuilt; the contended writer test advances its bounded fake clock.

Keeper ready-file startup code and its contract harness predate this round and
are not mirror wiring. The startup regression allows benign health retries while
checking every probe's child-owned endpoint (including non-200 and OSError
retries). Keeper mirror wiring remains a later slice.

Mirror test fixtures use worker-core’s shared Git test helper, including integration tests through the `test-support` dev feature. Hook Git redirects and host HOME/XDG configuration cannot select a repository outside their temporary fixture.

## Complete inventory refresh

`pg-repo-mirror REPOS.json MIRROR_ROOT` invokes `Mirror::fetch_all`, refreshing
every configured repository in stable name order even if an earlier fetch fails.
Each JSONL output row contains its repo and accepted watermark or `force_push`,
`fetch_failed`, or `fetch_timed_out` alarm. Any failure makes the command exit 1 after attempting the
whole inventory. Per-repo quarantine, locks and monotonicity remain unchanged.

The user timer in `deploy/systemd/pg-repo-mirror.{service,timer}` schedules this
command every minute. It reads `~/.config/plangraph/repos.json` and stores under
`~/.local/state/plangraph/mirrors`. Each fetch runs in a separate Rust worker using the same native gix backend.
A per-repository deadline kills and reaps a stuck worker (its process group on
Linux), records `fetch_timed_out`, and proceeds to the next repository. The default
limit is the smaller of five seconds and 45 seconds divided by inventory size;
`fetch_all_with_timeout` permits an explicit per-repository bound. The outer
55-second service deadline remains a final guard. Monitor service errors and
per-repository watermark age. Systemd never overlaps this oneshot with itself.
Installing/enabling the timer is a live operator step:

```sh
cd rust
cargo build --release -p pg-ctx --bin pg-repo-mirror
install -Dm755 target/release/pg-repo-mirror "$HOME/.local/bin/pg-repo-mirror"
cd ..
install -Dm644 deploy/systemd/pg-repo-mirror.service "$HOME/.config/systemd/user/pg-repo-mirror.service"
install -Dm644 deploy/systemd/pg-repo-mirror.timer "$HOME/.config/systemd/user/pg-repo-mirror.timer"
systemctl --user daemon-reload
systemctl --user enable --now pg-repo-mirror.timer
systemctl --user start pg-repo-mirror.service
journalctl --user -u pg-repo-mirror.service -n 50 --no-pager
```

These units are supplied, not installed. Keeper HTTP exposure and FLOW-WATCHDOG
alarm ingestion remain separate wiring; JSONL journal rows are not proof of an
HTTP endpoint or an installed stale-mirror alarm.

The `pg-repo-mirror` command uses itself as its fetch worker. Library callers of
`fetch_all` must install that executable on PATH or pin its path with
`with_fetch_worker`. Single-repository `fetch`/`fetch_named` remain direct native
operations. Private request/result files carry worker data; subprocess arguments
contain only their paths. A killed worker cannot publish later; abandoned staging
is swept by the next locked fetch, with the existing crash-recovery guarantees.
The deadline regression uses a hanging fake fetch-worker executable followed by
a real local fetch; no Git fetch subprocess or network service is introduced.
