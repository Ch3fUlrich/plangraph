Fix writer worktree creation on Windows by passing native, non-verbatim paths to Git.
Bundle test fixtures now record Git's actual clone remote URL rather than assuming native separators.
Exact remote verification and Unix worktree arguments remain unchanged.
