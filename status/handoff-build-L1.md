# build-L1 handoff 2026-10-25 ~20:00Z (gen-34 -> gen-35, routing RECYCLE after 1 compaction)

Read first: status/build-L1.card.md. J = /home/s/.claude/jobs/0bbdff0b/tmp (scripts, briefs, seat dirs). Notes: status/notes-build-L1.md. Cost model: status/direct-edit-cost-model.md.
FIRST:
- The hb loop runs detached (setsid hb.sh 90, about 12 h, log $J/hb.log). Check `tail -3 $J/hb.log`; restart only if stale >10 min.
- Keys: ops/hb-keys.txt (SQ12, KEEPER-PG-RECONNECT, SPAWN-WIN-PARITY, ORCH-EFFICIENCY-ENFORCE).
- Live state: `PYTHONPATH=~/code/plangraph-worktrees/spawner-host-keys/src python3 -m plangraph.spawner.cli ps`, then `gh pr list -R Ch3fUlrich/plangraph`.

Spawning:
- Spawn ONLY via `$J/gspawn2.sh <wt> <brief> <tag> <reason> <leg>` (legs mimo-v2.6-pro, agy-gemini-3.8-flash). The host gate is inside it.
- Budget: 3 of my runs (D-2840).
- Stall watch: `$J/stallwatch.sh <tag>` cancels after 45 min with no worktree write and no opencode.log activity (D-2845).

Rules new this gen:
- D-2835 + operator: the orchestrator makes fully specified changes itself, as a Hand-Edit commit. The gate cap is 10 lines today; it goes to 50 with hecap50 (D-2853) and to 400 plus a `Route-Decision:` trailer with the OEE slice. It never touches AUTH_DOOR_PATHS (keeper/*, auth, seat_verdict, pg.sh, ci_commit_gate, .github). Check the gate's REAL exit code.
- D-2853: OEE criteria 1-2 = the cost+L*time model.
- Evidence needs the FULL 40-char sha. `git rev-parse` it; never type one.
- Writer trailer: the brief says the literal RUNID. Fill it with filter-branch over ONLY the writer commits, before merging main.
- Post seats from a main checkout: `$J/postm.sh` uses ~/code/plangraph-worktrees/post-main (detached main 7bbd11a6; `git -C .. checkout --detach origin/main` first).

## In flight (owner build-L1 unless stated)
1. **#210 SPAWN-WIN-PARITY S1 (HIGH).**
   - Fix r1b run-eb06bed7 (mimo-pro, tag swp-s1f1c, brief $J/brief-swp-s1-fix1c.md F2-F6+LOWs, log $J/swp-s1f1c.log), worktree spawn-win-parity.
   - The local F1 hand-edit f5f2f027 is NOT pushed.
   - On done: check each finding, RUNID-fill over f5f2f027..HEAD, push, CI, delta seats qwen + Haiku (fixer xiaomi, writer google), GO-REQ per sha. Then spawn S2 (design §4 "### S2 brief"; prompt via stdin/file).
2. **hecap50 (OEE interim, D-2853).** Spawn right after the S1 fix:
   `bash $J/gspawn2.sh ~/code/plangraph-worktrees/hand-edit-cap-50 $J/brief-hecap50.md hecap50 "D-2853 interim hand-edit cap 10->50" mimo-v2.6-pro`
   - HIGH: 2 seats from different families including a non-Claude one, then GO-REQ with sha + CI run.
3. **#213 S3.**
   - Haiku seat: fix, 2 MED.
   - qwen seat queued ($J/seatswps3.sh, bg).
   - Then ONE mimo-pro fix round: $J/brief-swp-s3-fix1.md. Replace the QWEN_FINDINGS line with qwen's findings verbatim; Test-Removal D-2851 is already in it. Design §3 line 92 is already updated.
   - Then delta seats qwen + Haiku and judge GO.
4. **rvpc** (parser widening: REQUEST_CHANGES / ACCEPT / approved / "|" cell): queued, worktree review-verdict-parse-c, brief $J/brief-rvp-c.md.
   - REVIEW-VERDICT-PARSE is DONE, so ask routing which item owns it before the PR.
5. **KEEPER-PG-RECONNECT S2:** worktree keeper-pg-reconnect-s2 is ready; brief $J/brief-pgr-s2.md (agy). Spawn after the OEE slices (D-2835 order). A live keeper redeploy needs its own GO.
6. **ORCH-EFFICIENCY-ENFORCE (prio 10, claimed epoch 2).** #214 merged 7bbd11a6, evidence ok. E-slices follow build-L1-efficiency's design (D-2837); spec d97b5fb3 forwarded.

## Router-stall lines (owner / next step)
- **#210 idle 3.1h** -> build-L1: fix r1b running now (item 1); S2 follows its merge.
- **#200/#201 idle 8.2h** (SPAWN-ISOLATION-INPLACE / SPAWN-ORPHAN-REAP) -> build-L1: queued after KEEPER-PG-RECONNECT S2. Re-spawn mimo-pro in worktree spawn-iso-w (briefs/lanes-d2657/iso.md, fix-iso.md).
- **REVIEW-METHOD-SETTING in_review 13.1h** -> BLOCKED on a seat family.
  - Posting the Haiku LOW-only seat (parser now PASS) gets keeper 409 `same_family`, because build-L1's Claude Hand-Edits (D-2798, D-2817) make anthropic a writer family.
  - Writer families: google, xiaomi, openai, anthropic. alibaba (qwen) has already posted.
  - Next: ONE delta seat from a new family (opencode free glm-5.3-flash-free) or a router ruling. Sent to routing.
- **RT-P1-EXEC-OWNERSHIP in_review 39.6h** -> waits on its fix item SQ12 (D-2703, 4 findings).
  - SQ12 branch lane/exec-own-sq12 f308086f = WIP from a truncated mimo writer (45+/9-, finding 1 only).
  - Next: a mimo-pro continuation writer on coding.vm (keeper/* = AUTH_DOOR, writer only) for findings 2-4, then 2 seats and GO.
  - No cancel proposed.
- **CI-SELECT-DEPLOY-REFS** -> no action; closes via E5 (refuted-seat posting).

## Gaps for routing / LESSONS
- **Direct edits vs seat families:** each Claude Hand-Edit adds anthropic to the item's writer families, so a Haiku seat is then refused (same_family). With direct edits now preferred, Haiku stops being a usable seat. The seat algorithm needs a rule, for example counting Hand-Edits of <= N lines as non-authoring or always requiring 2 non-Claude seats.
- **ci_commit_gate cannot be edited by the orchestrator** (AUTH_DOOR + no Claude Writer). That is correct, but every gate tweak costs a writer run.
- **Run records have no token counts:** the cost model can't be measured until they do (OEE pt 10).
