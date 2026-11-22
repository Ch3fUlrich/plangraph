# Batch 71 spawner follow-ups

The batch status records three deferred items and two deferred parts of the
writer wait-loop item. The companion `slices.json` in
the batch review directory contains the seven Keeper definitions under
`plangraph:PH-ORCH-GUARDS`.

- **SKIPPED plangraph:LE-REVIEW-BUILDS-FILL-CODING-VM-DISK.** Worker admission,
  receiver-owned checkout and build cleanup, and a post-landing sweep span
  three lifecycles. The slices are `REVIEW-WORKER-HOST-PLACEMENT`,
  `REVIEW-WORKSPACE-LEASE`, and `REVIEW-LANDING-WORKTREE-SWEEP`.
- **DELIVERED plangraph:LE-WRITER-WAIT-LOOP-PGREP-MATCHES-ITSELF (Bash guard).**
  The Bash hook rejects literal `pgrep -f` patterns that match the shell
  running the wait loop. The guard and its fixtures are in `46a81642c` and
  `d5a5d1760`.
  - **SKIPPED WRITER-STOP-OWNED-TREE.** Session stop needs an owned process
    identity captured at launch before it can safely reap child processes.
  - **SKIPPED WRITER-DELETED-CWD-ALERT.** Alert coverage for old shells with a
    deleted working directory needs its own process fixture.
- **SKIPPED plangraph:LE-CLAUDE-WRITER-BRIEF-SKIPS-PUSH-SCRUB.**
  `pg-spawnd` is an in-scope generator entry point. Its current scrub is in
  the native Codex driver's post-turn gate, while the Claude session launcher
  accepts caller-authored prompt text and has no batch card, base pin, or
  completed-head check contract. A shared renderer alone would leave that
  Claude launch path able to omit the scrub. `WRITER-BRIEF-SCRUB-PARITY` wires
  both launch paths to shared batch inputs and checks, then covers brief
  parity and scrub rejection at the completed head.
- **SKIPPED plangraph:LE-HANDOFF-EDITED-AFTER-READY-RECYCLE-REFUSED.** The
  deployed ctx-watchdog runs from a separately pinned router checkout.
  `HANDOFF-READY-SNAPSHOT` covers the marker producer, watchdog consumer,
  successor input, and late-note digest in one fixture.
