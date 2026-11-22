# Batch 71 spawner follow-ups

The batch status records four deferred items. The companion `slices.json` in
the batch review directory contains the seven Keeper definitions under
`plangraph:PH-ORCH-GUARDS`.

- **SKIPPED plangraph:LE-REVIEW-BUILDS-FILL-CODING-VM-DISK.** Worker admission,
  receiver-owned checkout and build cleanup, and a post-landing sweep span
  three lifecycles. The slices are `REVIEW-WORKER-HOST-PLACEMENT`,
  `REVIEW-WORKSPACE-LEASE`, and `REVIEW-LANDING-WORKTREE-SWEEP`.
- **SKIPPED plangraph:LE-WRITER-WAIT-LOOP-PGREP-MATCHES-ITSELF.** The Bash guard
  is covered by its hook tests. Process-tree ownership at stop and alerting on
  old shells with a deleted working directory remain in
  `WRITER-STOP-OWNED-TREE` and `WRITER-DELETED-CWD-ALERT`.
- **SKIPPED plangraph:LE-CLAUDE-WRITER-BRIEF-SKIPS-PUSH-SCRUB.** The
  fleet-launch Claude writer entry point and the legacy writer script are
  outside this checkout. `WRITER-BRIEF-SCRUB-PARITY` covers their integration
  with the shared checks and the scrub rejection fixture.
- **SKIPPED plangraph:LE-HANDOFF-EDITED-AFTER-READY-RECYCLE-REFUSED.** The
  deployed ctx-watchdog runs from a separately pinned router checkout.
  `HANDOFF-READY-SNAPSHOT` covers the marker producer, watchdog consumer,
  successor input, and late-note digest in one fixture.
