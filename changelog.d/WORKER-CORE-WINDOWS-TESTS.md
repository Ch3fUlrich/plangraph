Fix writer worktree creation on Windows by passing native, non-verbatim paths to Git.
Bundle test fixtures now record Git's actual clone remote URL rather than assuming native separators.
Exact remote verification and Unix worktree arguments remain unchanged.
Windows worker-core: use real exclusive file locks for fleet ticks, accept native absolute publish remotes, and canonicalize cleanup fixtures.
Read owner records with pinned, non-reparse Windows paths; retain Unix locking and confinement behavior.
