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

There is no caller and no push. Bundle transport, launch verification, child
environment and writer ownership remain later R4 slices; finalization and
publication remain R5–R7. Rollback uses `git revert`.
