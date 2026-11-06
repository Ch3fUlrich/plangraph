# Workspace resolver: R4A

`workspace_resolver::ensure_commit` checks a registry repository for a full
lowercase commit SHA before taking any lock. On a missing object it locks the
canonical Git common directory's persistent `spawner-fetch.lock`, rechecks the
object, validates the origin URL against the registry, and fetches only origin.
Linked worktrees share this lock. Missing after fetch is `sha_not_reachable`.
Other Git failures fail closed; diagnostics never display stderr or environment.

## Object classification

A valid object ID that is missing or cannot peel to a commit (including a blob,
tree, or tag targeting a non-commit) follows the fetch/recheck path and returns
`sha_not_reachable` if still unavailable. Tags peeling to commits remain valid.
Failed Git execution or malformed object-probe output remains `git_failed`.
Warm-path tests hold the fetch lock and use an unusable registry remote; an
existing `FETCH_HEAD` is allowed and is not evidence of a resolver fetch.

Git is invoked directly, with separate arguments, registry cwd, inherited
`GIT_*` variables removed and prompting disabled. The existing cross-platform
lock helper releases the lock on every exit. The registry and Git administration
directory are trusted. Never unlink or replace the lock anchor: OS file locks
cannot protect a substituted inode.

## R4B: bundle transport

Export uses an exclusive temporary `refs/spawner/bundle/<run_id>` tip and removes
it afterwards. Prerequisites are the merge-base with origin's default branch,
not the requested base: unpushed base and head both travel. Import fetches origin,
verifies prerequisites and accepts only the expected single tip; it imports
objects, not branches. Origin fetches disable hooks and recursive submodules.
The shared `writer_secret::SecretGate` scans complete reachable blobs and commit
messages, not compressed bundle bytes. Its byte mapping preserves binary input
and ASCII secret boundaries. Git object identifiers are metadata, not text to
redact. Sanitized history reconstruction preserves mapped merge parents and safe
messages, replaces secret-bearing messages, and appends `Sanitized: secret-gate`.
It returns the rebuilt tip, SHA mapping and dropped paths, and refuses secret-bearing
prerequisite history through the same gate. Salvage export is outside this slice.

Neither transport pushes. R5 must scan reachable blobs and commit messages,
then re-scan imported content; serialized bundle scanning is insufficient.

## R4C: verification

Verification checks canonical toplevel/common-dir/admin-dir, registry remote,
HEAD, clean tree, requested branch or detached mode, and effective autocrlf
(plus Windows longpaths). The supervised Python canary requires the imported
file inside the checkout; shared editable installs cannot satisfy this check.
Ignored files are deliberately allowed: build outputs and local virtualenvs may
exist. Consequently an ignored package inside the checkout can shadow a tracked
package. The canary proves location, not content provenance; R5 admission must
gate ignored executable content too, or supply a freshly cleaned owned tree.

## R4D: launch environment and writer ownership

Refreshed base: `1e8771af9c14f9b7415046c59fde18c19b47acde` (origin/main,
after both R4B and R4C merged). No existing Temporal attempt tree is reused.
`create_writer` creates exactly the requested fresh lane branch under the
canonical worktrees root. Existing branches/destinations refuse without force.
Worktree config is enabled under a common-dir config lock; shared autocrlf and
longpaths values remain unchanged; checkout follows worktree-local configuration.
Partial failures leave explicit state for
operator inspection, never automatic destructive cleanup of unverified paths.

`verify_owned` re-resolves identity independently of clean HEAD and requires an
exclusive regular marker in the Git admin directory. RepoEntry has no repo_id:
the marker binds canonical registry root (repository identity) and checkout.
A checkout-local marker never counts. R5 must repeat this proof immediately
before every reset, clean or removal. Same-user malicious races remain a
sandbox/trust-model limitation, not a claimed isolation boundary.

`apply_child_env` applies sandbox confinement even to a plain command, preserving removed
variables. All git location variables are removed; cwd/PWD and
canonical import-root-only PYTHONPATH are pinned using platform path joining.
The shared `configure_import_environment` helper pins `PYTHONDONTWRITEBYTECODE=1`
even over explicit command overrides. Bytecode and pytest caches are disabled,
TMPDIR/TEMP/TMP pinned outside the worktrees root, and safe seat slots
expand cache templates outside worktrees. Canonical ancestors prevent symlink
escape. `prepare_launch` verifies before configuring the command; none mode requires no checkout
and invents no cwd or PYTHONPATH. The standalone canary uses the same builder
with a canary slot and system temp; the launch child uses caller per-run temp.

No daemon caller exists. Lifecycle, result bundles, keeper coordination,
publication and live acceptance remain R5–R7. Rollback uses `git revert`.
# Writer result boundary

`writer_result::check_result` checks owned writer lanes after execution, including
cleanliness, ancestry and terminal Writer trailers on every new commit.
`export_result` requires a reachable-content gate before R4 bundle export and
seals bytes through `BlobStore`. It never pushes or removes a checkout.
Base and head are lowercase 40-hex object IDs, never revision expressions.
Uppercase hex digits are rejected explicitly. None-mode launch permits an absent
worktrees root without creating it; ancestor resolution still rejects traversal
and temporary directories inside the resolved root, including symlink aliases.
The none-mode path regression covers a true outside-base `../escape`, lexical
`missing/../escape`, and an allowed existing nested root, without relaxing the
fail-closed rejection of parent components.
Bundle roundtrip tests cover base and head absent from origin; a separate-origin
test verifies that export does not publish the writer lane.
`LocalBlobStore` uses SHA-256, a 64 MiB limit and immutable execution sidecars;
the local R6 store location is `state/blobs`. Sealing is not a keeper receipt or
attempt completion. This slice performs no garbage collection. Referenced
pending/conflicted results must not expire; finalized results receive salvage
retention (seven days or acknowledgment), and only unreferenced orphan blobs
use a 24-hour retention period.
