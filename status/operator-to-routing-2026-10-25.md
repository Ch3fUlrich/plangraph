## Operator orders to routing (2026-10-25, plangraph efficiency; extends D-2835)

The current speed and token/money spend are not acceptable. Enforce the following for every L1/L2 and in code. A prose rule only counts once the algorithm enforces it.

1. **Priority.** Set `plangraph:ORCH-EFFICIENCY-ENFORCE` to priority 10 (the highest). It goes before every other plangraph item except the SPAWN-WIN-PARITY fix already running.
2. **Small changes by the orchestrator itself.** I have said this several times: a Claude orchestrator makes a small change directly (own commit, Hand-Edit trailer) instead of spawning a writer, briefing it and running a handoff and review loop. Cost efficiency means the combined cost of all models plus spawn, brief, handoff, waits and seats. It does not mean "don't use Claude". Put this into the spawner `route` algorithm (`direct_edit` action) and into `ci_commit_gate`. It must not stay a prose rule.
3. **Tool choice is code.** Model and tool selection, direct edit included, is decided by the spawner algorithm. A tooling gap must never again cost router round trips (CI-SELECT-DEPLOY-REFS: 3 seats and 3 decisions for an already-merged change).
4. **Stalled runs are killed by code.** A run with no worktree write and no harness activity for 15 min is cancelled, its slot is freed, and the cancel is recorded. `stall_watchdog.py` today only flags sessions that leave finished runs unacknowledged, so it does not cover this.
5. **Session relaunch must be cheap.** Add these to ORCH-EFFICIENCY-ENFORCE as done criteria and slices, not as new items:
   - a) **One compiled boot file per level.** Superseded rules are removed, and a size cap is checked in CI. A relaunched session reads only that file plus the card, not the full skill, guide, memory list and handoff.
   - b) **Runs outlive sessions.** The spawner owns live runs and their completion events per owner/claim. A new session re-attaches with one command (for example `spawner watch --owner build-L1`) and gets every pending completion. No shell waits tied to a dead session.
   - c) **Live state is a command, not a snapshot.** One command shows runs, PRs, CI and claims fresh. The handoff holds only decisions and next steps.
   - d) **Claim heartbeats come from the keeper/spawner** while a run is alive, not from shell loops per session.
6. **The workstation back in use, fast.** SPAWN-WIN-PARITY is the critical path:
   - S1: mimo endpoint per run.
   - S2: prompt through stdin or a file instead of argv. Briefs and prompts live in files as best practice.
   - S3: retry on WinError 5.

   S3 now runs in parallel with the S1 fix, and S2 starts as soon as S1 merges. Raise build-L1's concurrent-run budget from 2 to 3. The host gate stays.
7. **Measure it.** Report tokens per merged change and time from claim to merge per item, so regressions are visible.
