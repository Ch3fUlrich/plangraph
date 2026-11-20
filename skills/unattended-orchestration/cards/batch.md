# Batch

Role: the batch-mode orchestrator and its writers. One strong writer takes several related
cards, starts an independent reviewer on its head and fixes findings in the same session.
The orchestrator combines accepted heads into one batch PR.

R-batch-01: Give one strong writer a brief of related cards with disjoint write sets, in one worktree at the fetched origin default head.
  Never split related ready items across writers, for any agent type; a batch of one is right only when no related item is ready.
  When one leg is out of quota the batch goes to the next implement leg; single-task (`piecewise`) runs follow the worker card.
  While Codex is not served, Claude Sonnet or Opus write batches, in Claude Code or through agy; no batch waits for a quota reset (routing DECISIONS 3.58 (2), D-6366).
  Launch with the agent-neutral writer driver (today plan-order `bin/writer.sh NAME WORKTREE BRIEF`; the spawner route picks the leg).
  (why: one context per change set avoids per-card handoffs and cold restarts; source: routing DECISIONS 3.56 (1) D-6357, plangraph:BATCH-PORT-WRITER-DRIVER, LE-BATCH-DRIVER-SINGLE-AGENT)
R-batch-02: Every brief names the goal, Plan-Item keys, scope files, checks, commit trailers, round cap and status file ending `<NAME> DONE`.
  (why: fixed fields let the driver check completion; source: briefs-0511/SKILLMODES-1.md, plangraph:BATCH-PORT-WRITER-DRIVER)
R-batch-03: Run fast checks on the exact head before review; require its fast-checks stamp before starting the seat.
  (why: review and CI must not find gate failures first; source: plan-order/bin/fast-checks.sh and review.sh)
R-batch-04: Start an other-family review with repository access on the exact head, fix confirmed HIGH/MED findings and repeat while progress permits.
  Native admission: `rust/pg-spawnd/src/round_progress.rs` records confirmed HIGH+MED per writer round.
  Stop rule (one owner): 3 rounds in a row that bring the count no lower than the best so far refuse the next writer with `no_progress`;
  from round 3 on a count still above half of the round-1 count refuses with `slow_progress`; round 9 always refuses with `round_cap`.
  See `docs/SPAWNER.md` for verdict-file binding and the single narrow fix exception.
  At the cap, close the PR unmerged and have the strongest available planner define slices <=800 changed lines (excluding Cargo.lock and generated files) with all open findings verbatim.
  Exception: one or two confirmed MED findings with clear fixes permit one narrow fix round. A HIGH, three or more MED, or a second miss closes and replans.
  (why: an in-session fix loop avoids relaunch cost; source: routing DECISIONS 3.35, D-5909, D-5927, D-5964)
R-batch-05: Reviews never wait; the first served leg wins, in this order: agy Gemini Argon (low effort), then mimo-v2.6-pro, then OpenCode DeepSeek flash.
  The reviewer's family differs from every writer family of the diff, so a Claude-written change never gets a Claude seat.
  Run them with the agent-neutral review command: today plan-order `bin/review.sh TAG BASE HEAD PROMPT` with WRITER_FAMILY and REVIEW_WORKTREE set.
  It spawns role review with the writer's family excluded in a no-remote checkout; long term `plangraph review` (plangraph:REVIEW-CMD).
  Credential, auth, access and secrets reviews require witnessed Sonnet high; every seat's family differs from all writers and its verdict names the exact head.
  Scripts and rules name the role, never one agent (review.sh, writer.sh).
  (why: reviews must progress cheaply on limits the writers do not use; source: routing DECISIONS 3.58 (1) D-6366, 3.56 (4) D-6357)
R-batch-06: Use `VERDICT: ACCEPT` or `VERDICT: REQUEST_CHANGES` for writer reviews, naming the full head sha in the review.
  For the batch merge-delta approval, request `HEAD: <full sha>` then `VERDICT: PASS` as its last two non-empty lines, as required by merge_batch land.
  Never translate a reviewer's verdict yourself.
  (why: the driver and merge gate require different verdict formats; source: plangraph:BATCH-PORT-WRITER-DRIVER, tools/merge_batch_land.py::check_approval)
R-batch-07: Admit runs only with at least 5 GB free on `/` on every host; stop running writers at 2 GB free, with no percentage threshold.
  Share one Cargo target per host. (why: duplicated targets filled the disk and killed writers; source: routing DECISIONS 7.1 D-5946, plangraph:LE-WORKTREE-CARGO-TARGET-FILLS-DISK)
R-batch-08: Place writers through spawner host admission and overflow using free RAM and disk, trying dell first; never use a host-local launcher.
  (why: a local launch left a free host idle; source: routing DECISIONS 7.1, plangraph:LE-PLAN-ORDER-WRITERS-BYPASS-HOST-PLACEMENT)
R-batch-09: Combine heads with accepted part reviews and green fast gates using `tools/merge_batch.py build` and `push`.
  Run one full CI at the batch head and one review of the merge delta (remerge-diff) before `land`.
  Single-PR exceptions require the exact token in the GO row: `EXCEPTION:main-red-fix` (main red, merger's own fix),
  `EXCEPTION:only-one-ready-pr` (one PR ready and no other), or `EXCEPTION:operator-order` (operator order for that PR).
  (why: one CI and one GO replace per-PR gates; source: routing DECISIONS 3.39 D-5938, tools/merge_batch.py)
R-batch-10: Send one GO request per batch naming its PR, full head sha, each part's review file and CI result; include the rule 3.17 block.
  Its exact format is `PLAN <keeper key> conforms=yes|no deviations=<text|none>`; `conforms=no` requires stated deviations.
  Gate/workflow edits (GATE_PATHS in tools/ci_commit_gate.py) require a router Gate-Edit D-id. (why: routing compares one patch per GO; source: plangraph:BATCH-PORT-GO-REQ, docs/ci-gate.md)
R-batch-11: Record every incident or workaround in the same beat: every session, router included, sends plan-order one line `LESSON <what> <evidence>`.
  plan-order attaches it to an existing LE item or defines a new one, names its writer run and starts the fix.
  (why: unrecorded lessons repeat; one converter keeps one item per root cause; source: routing DECISIONS 2.12 D-5852, 3.56 (2) D-6357)
R-batch-12: After merge, close merged items with GO and review evidence; sweep their branches and clean, idle worktrees using a restore list.
  Preserve dirty trees, unpushed work and live or unit-referenced worktrees.
  (why: stale worktrees waste disk and obscure live work; source: plangraph:MERGED-WORKTREE-BRANCH-SWEEP, LE-CLOSE-MERGED-AFTER-BATCH-MERGE)

## Port to code

Keys below are in the `plangraph:` namespace. Work-mode ports are dependencies of
plangraph:WORK-MODE-SETTING; R-batch-11 uses the cross-cutting LESSON-TO-GATE item.
Rule 2.12 places incident-specific LE items under LESSONS-ENFORCE. These are implementation targets, not claims that the gates
already ship. When a port lands and enforces the rule, replace its procedure with a code pointer.

| Rules | Keeper item / existing code |
| --- | --- |
| R-batch-01..04, 06 | BATCH-PORT-WRITER-DRIVER, LE-BATCH-DRIVER-SINGLE-AGENT |
| R-batch-05 | REVIEW-CMD, ROUTE-ORDER-D6310, MIMO-BATCH-CAPACITY-D6357 |
| R-batch-07 | LE-WORKTREE-CARGO-TARGET-FILLS-DISK, HOSTS-RUST-S5-DISK-HYGIENE |
| R-batch-08 | LE-PLAN-ORDER-WRITERS-BYPASS-HOST-PLACEMENT |
| R-batch-09 | `tools/merge_batch.py` (existing code), WORK-MODE-S4-CONSUME |
| R-batch-10 | BATCH-PORT-GO-REQ |
| R-batch-11 | LESSON-TO-GATE (cross-cutting) |
| R-batch-12 | MERGED-WORKTREE-BRANCH-SWEEP, LE-CLOSE-MERGED-AFTER-BATCH-MERGE |

R-batch-13: Freeze `land` and `merge-part` while main's latest tests run is not
  completed green on main's tip. Only a GO row naming the exact head and token
  `EXCEPTION:main-red-fix` waives the freeze (`merge_batch land --go-row PATH`);
  the candidate still needs its own full green CI and reviews. Trial runner
  variables on a non-main branch with a full green workflow_dispatch run first,
  and record that run, head and labels in the D-row before switching runners.
  See docs/ci-parallel.md for the procedure; router-owned merge-part is external.
  (why: a red main tip hides each new batch's regressions; source: plangraph:LAND-FREEZE-ON-RED-MAIN, docs/ci-parallel.md)

R-batch-14: Every writer commit fixing or implementing keeper items carries one
  `Plan-Item` trailer per fixed key, even within a larger item. Warn on named keys
  missing from commits with `ci_commit_gate.py check --base BASE --head HEAD
  --item-text PART.txt`; CI reads the PR title/body from the GitHub event.
  Coverage policy: `rust/pg-policy/src/plan_items.rs`, called by `tools/ci_commit_gate.py`.
  (why: parent-only trailers prevent close-merged proof; source: D-6054)
R-batch-15: Before plan-order sends DONE, run `pg close-merged KEY GO-D-ID --from-open`
  without `--apply` for every item being reported DONE. Require WOULD-CLOSE; on
  refusal report the key and exact refusal reason, keep it open, and do not claim DONE.
  (why: merged fixes need verifiable keeper evidence; source: D-6054)
