# Status card — plangraph-L1-main (Opus 5.5 high, L1 project orchestrator; relaunched fresh 2026-10-01 ~20:15)
Updated: 2026-10-02 10:10 UTC | Brief: routing/docs/plan/briefs/plangraph-L1-main.md | Constraints: CONSTRAINTS.md | Handoff: docs/HANDOFF.md

## Phase
M1 CLOSED. M2a code accepted (routing-00 Opus spot review). Now: X6 fixes -> X7 tiers -> 2-seat re-review -> pilot keeper -> plan-v3 load ->
router batch -> render --cdn -> M2c research + proposal (D-231). M2b / M3 / M4 not started.

## State (branch worktree-proposal, worktree .claude/worktrees/proposal)
- Code = 1ef63ee (497 pytest green). Writers' code-only repo: /home/s/.claude/jobs/33c5cb14/tmp/keeper-code (master 6bb2133, R-coord-01).
- X6c MERGED into worktree 380683c (keeper-code 92ecd11; 538 green): epoch-scoped change streak, cancel clears waiver, family
  charset, waiver bound to `Waives: <key>` D-row, residual-risk doc. Writers qoder (qfmodel, family unresolved, client-reported).
  Seats on X6c: seat1 muse-spark (205156), seat2 gemini-3.8-flash via gemini client (205302; trial leg before paid deepseek, D-242).
- Writer leg: qoder client (promo). Unservable: ovh-*, groq-qwen, vertex-gemini-3.8-flash ("Unable to determine provider"); deepseek 504s.
- KEEPER-X7 ACCEPTED (D-312), merged f8bb3e5 (720 green; seat record docs/reviews/2026-10-02-keeper-x6c-x7-seats.md).
- PILOT KEEPER RUNNING (coding.vm 127.0.0.1:8787, "isolation not effective (pilot)"; config ~/.local/share/plangraph/keeper.json,
  pid in daemon.pid (stop by PID, never pkill -f); tokens ~/.config/plangraph/{router,plangraph-L1-main}.token 0600). plan-v3 loaded
  (93 items, D-314 fold of 6 router defs, c698570), verify ok, 19 ready. Page out/plan-cdn.html -> routing (private publish).
- Router batch (76 ops) + server batches 1-5 (@50a2a68, end-state clean) BLOCKED until apply_ops: per-op validation refuses new leaves
  (unreachable_goal). Phase KEEPER-BATCH (D-316) = interactive L2 plangraph-L2-keeper-batch (brief tmp/L2-keeper-batch-brief.md):
  apply_ops + L5 Closes fail-closed + unblocks column + RUNBOOK python backup.
- M2c-RESEARCH DONE (573dc6c+6c68e99). M2c phase 1 = code/data split (fresh public history; D-300). Reqs D-259/276/290/292/300 in task notes.
- Tools: tools/plancheck.py (end-state pre-check), docs/PLAN-FORMAT.md (L1 plan format, D-296), tmp/d272check.py.
- Task contracts ready: $CLAUDE_JOB_DIR/tmp (= /home/s/.claude/jobs/7caaf7f5/tmp) hdr.txt + x6c1-3.txt, x7a1, x7a2, x7b, x7c (+ X7d batch applier TBD).
- Router batch DRAFT: data/updates/2026-10-01-router.yaml (61a9fb9; 82 ops, 93->118 items, ready 19->31, checks pass; checker
  $CLAUDE_JOB_DIR/tmp/batchcheck/check.py). Sent to routing-00 for early look with 3 questions (T0-WSCLONE reopen verb, decision-only
  items' tiers, T5-GROK-L2 dep). NOT loaded; needs applier (X7d) + pilot keeper + routing-00 OK.
- NOTHING loaded into a real store yet.

## L2 launch (D-285): new phases = brief file + `L2-LAUNCH plangraph-L2-<phase> <brief> <cwd>` to routing; L2 reports by
   SendMessage; I send `L2-DONE <name>`. KEEPER-X7 r4 (in-session bg L2, D-284 CLI path + gate) finishes as is.
## D-297 Claude-saving: legs free -> trial (vertex flash 3.6-3.8, OVH) -> paid (spark, deepseek); no Sonnet seats unless no
   non-anthropic family serves the tier; no Sonnet research; small L2 contexts.
## Waiting on
- L2 results (KEEPER-X7, M2c-RESEARCH). Router batch load waits for: X7 + seats + pilot keeper + routing-00 OK on formal diff.
## Claude subagent use: 83,465 tokens (Sonnet: router batch draft); keep small, free/trial seats first (D-253). Compactions this session: 0 (report each to routing; fresh restart after the 5th).
## Spend: pending ledger (gateway usage unreadable until OS-41). Paid run ids: see HANDOFF §2 + this session's x6c*/x7* titles.
## Notes: never read opencode session DBs; never print tokens; heavy runs -> workstation via routing-00.
