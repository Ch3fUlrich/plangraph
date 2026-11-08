Harden writer Git configuration protection: suppress repeated missing-snapshot
alerts, preserve current completion diagnostics, explicitly reject run-layout
mismatches, normalize linked-worktree backlinks across Windows/WSL spellings,
and restore or remove config.worktree files including symlink replacements.
Fail closed on unresolved backlinks, preserve path anchors and Windows case semantics, retain records on layout errors including retries, and use current orphan diagnostics.

Round 3: reject extended UNC backlinks, require bilateral Windows case folding, retain layout-error save markers, and cover accepted backlinks and production orphan settlement.
Round 4: restore drive/WSL anchor and traversal coverage, preserve id-selected orphan exit diagnostics, and share Windows-safe file and Unix-only directory syncing.
- Restore the layout-error test that keeps the record and run directory (D-4888).
- Restore created_during_run_removed, non_checkout_noop and record_api_always_supplies_a_parent (seat findings, round 4).
