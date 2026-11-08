### Fixed
- pg-spawnd privately snapshots writer repository common and per-worktree Git
  configuration before launch, including absent files. Normal completion and
  restart reconciliation restore changes atomically and mark the run failed
  with `gitconfig_tampered`, preserving the original outcome separately.
- Status records and events expose changed paths only, never configuration
  contents. Snapshots survive daemon restarts in private run sidecars.
