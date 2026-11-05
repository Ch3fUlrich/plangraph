# Workspace resolver: R4A

`workspace_resolver::ensure_commit` checks a registry repository for a full
lowercase commit SHA before taking any lock. On a missing object it locks the
canonical Git common directory's persistent `spawner-fetch.lock`, rechecks the
object, validates the origin URL against the registry, and fetches only origin.
Linked worktrees share this lock. Missing after fetch is `sha_not_reachable`.
Other Git failures fail closed; diagnostics never display stderr or environment.

Git is invoked directly, with separate arguments, registry cwd, inherited
`GIT_*` variables removed and prompting disabled. The existing cross-platform
lock helper releases the lock on every exit. The registry and Git administration
directory are trusted. Never unlink or replace the lock anchor: OS file locks
cannot protect a substituted inode.

There is no caller and no push. Bundle transport, launch verification, child
environment and writer ownership remain later R4 slices; finalization and
publication remain R5–R7. Rollback uses `git revert`.
