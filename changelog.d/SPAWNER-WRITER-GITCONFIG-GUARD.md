### Fixed
- pg-spawnd privately snapshots writer repository common and per-worktree Git
  configuration before launch, including absent files. Normal completion and
  restart reconciliation restore changes atomically and mark the run failed
  with `gitconfig_tampered`, preserving the original outcome separately.
- Status records and events expose changed paths only, never configuration
  contents. Snapshots survive daemon restarts in private run sidecars.
Round 2 hardens pointer resolution (canonical, symlink-free administrative paths,
linked-worktree layout and backlink, enclosing submodule modules directory),
refusing unsafe capture with `gitconfig_capture_refused` before launch. Snapshots
and pre-restore verdicts are synced; missing snapshots have a distinct failure
reason. All non-dry roles are guarded, restoration is verified with one retry,
and event delivery failures are recorded. Restart, settle, submodule, poisoned
pointer, missing-sidecar and symlink regression tests cover the failure paths.
