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

## In flight (owner build-L1 unless stated); each bg job below writes its log in $J and survives this session
1. **#210 SPAWN-WIN-PARITY S1 (HIGH).**
   - Fix r1b is done (run-eb06bed7, 8 commits F2-F6 + LOWs + sweep, RUNID filled). Merged main once; head 7b8b3e33 pushed; 411 tests passed; gate rc 0; I6 is still in src (opencode_render.py:16).
   - PR CI: check `gh pr checks 210`.
   - qwen delta seat: running `$J/seatswps1d.sh` (log $J/seatswps1d.log, dir $J/seat-swps1d-qwen; delta 0d3a7d99..0518aaf3, vectors $J/swp-s1d.vec).
   - **D-2857: drop f5f2f027; the next mimo-pro round writes those 3 hermetic-fixture lines, so Haiku is eligible as the second seat.**
     - f5f2f027 is already pushed, and force-push is forbidden, so build a NEW branch, the same way #211 became #212:
       `git -C ~/code/plangraph-worktrees/spawn-win-parity checkout -b lane/spawn-win-parity-b 0d3a7d99 && git cherry-pick <the 8 writer commits after f5f2f027, oldest first>`.
       The cherry-pick of 19d00082 (the I16 conftest scrub) may need f5f2f027's context: resolve by keeping the writer's lines only.
     - Then spawn mimo-pro with a 1-finding brief that says verbatim: "Add an autouse fixture in tests/test_spawn_cli.py and tests/test_w9_regressions.py that sets PLANGRAPH_GATEWAY_URL to http://127.0.0.1:9/v1 via monkeypatch (hermetic: I6 refuses a gateway leg with no base URL); D-2857: this replaces L1's dropped Hand-Edit f5f2f027". Leg mimo-v2.6-pro.
     - Then RUNID fill, merge main once, push, open a new PR, and close #210 with a pointer.
     - Seats: the qwen delta seat on 7b8b3e33 still counts for the parts that do not change. Haiku is the second seat on the new head. Then GO-REQ per sha.
     - NO Claude commits on this branch.
   - Then GO-REQ per sha and spawn S2 (design §4 "### S2 brief"; prompt via stdin/file).
2. **#215 hecap50 (OEE interim, D-2853/D-2856).** Head 30679bb7 (writer run-a57cee93; trailers Gate-Edit + Test-Removal D-2853), gate rc 0, 107 passed.
   - Seats: Haiku + qwen running `$J/seathecap.sh` (log $J/seathecap.log).
   - Then GO-REQ with the exact head + CI run, then merge with the sha, then full-sha evidence on OEE.
3. **#213 S3.**
   - Fix r1 is running (`$J/gspawn2.sh` tag swps3f1, log $J/swps3f1.log, brief $J/brief-swp-s3-fix1.md: Haiku + qwen findings verbatim; Test-Removal D-2851).
   - On done: RUNID fill over 33d65068..HEAD, merge main once, push, CI, delta seats qwen + Haiku (writer google, fixer xiaomi, no Claude edits), judge GO.
4. **rvpc** (parser widening: REQUEST_CHANGES / ACCEPT / approved / "|" cell): queued (`$J/gspawn2.sh` tag rvpc), worktree review-verdict-parse-c, brief $J/brief-rvp-c.md.
   - REVIEW-VERDICT-PARSE is DONE, so ask routing which item owns it before the PR.
5. **KEEPER-PG-RECONNECT S2:** worktree keeper-pg-reconnect-s2 is ready; brief $J/brief-pgr-s2.md (agy). Spawn after the OEE slices (D-2835 order). A live keeper redeploy needs its own GO.
6. **ORCH-EFFICIENCY-ENFORCE (prio 10, claimed epoch 2).** #214 merged 7bbd11a6, evidence ok. E-slices follow build-L1-efficiency's design (D-2837); spec d97b5fb3 forwarded.

## Router-stall lines (owner / next step)
- **#210 idle 3.1h** -> build-L1: fix r1b running now (item 1); S2 follows its merge.
- **#200/#201 idle 8.2h** (SPAWN-ISOLATION-INPLACE / SPAWN-ORPHAN-REAP) -> build-L1: queued after KEEPER-PG-RECONNECT S2. Re-spawn mimo-pro in worktree spawn-iso-w (briefs/lanes-d2657/iso.md, fix-iso.md).
- **REVIEW-METHOD-SETTING in_review 13.1h** -> BLOCKED on a seat family.
  - Posting the Haiku LOW-only seat (parser now PASS) gets keeper 409 `same_family`, because build-L1's Claude Hand-Edits (D-2798, D-2817) make anthropic a writer family.
  - Writer families: google, xiaomi, openai, anthropic. alibaba (qwen) has already posted.
  - D-2855/D-2856: the glm seat failed at 20:15Z on "Free usage limit reached" (run-b7122767); reset time unknown. Retry ONCE after the reset (try about 24 h later, or when an `opencode run -m opencode/glm-5.3-flash-free` smoke stops printing the limit); no swap, no paid path. Command: `$J/glmseat.sh $J/seat-rms-glm rmsglm`, log $J/rmsglm.log, neutral prompt. On done: `bash $J/postm.sh $J/seat-rms-glm plangraph:REVIEW-METHOD-SETTING "google --writer-family xiaomi --writer-family openai --writer-family anthropic"`.
- **RT-P1-EXEC-OWNERSHIP in_review 39.6h** -> waits on its fix item SQ12 (D-2703, 4 findings).
  - SQ12 branch lane/exec-own-sq12 f308086f = WIP from a truncated mimo writer (45+/9-, finding 1 only).
  - Next: a mimo-pro continuation writer on coding.vm (keeper/* = AUTH_DOOR, writer only) for findings 2-4, then 2 seats and GO.
  - No cancel proposed.
- **CI-SELECT-DEPLOY-REFS** -> no action; closes via E5 (refuted-seat posting).

## Gaps for routing / LESSONS
- **Direct edits vs seat families:** each Claude Hand-Edit adds anthropic to the item's writer families, so a Haiku seat is then refused (same_family). With direct edits now preferred, Haiku stops being a usable seat. The seat algorithm needs a rule, for example counting Hand-Edits of <= N lines as non-authoring or always requiring 2 non-Claude seats.
- **ci_commit_gate cannot be edited by the orchestrator** (AUTH_DOOR + no Claude Writer). That is correct, but every gate tweak costs a writer run.
- **`spawner route --role review` names qwen's family `qwen`**, while the keeper calls it `alibaba`, so `--writer-family alibaba` does not exclude it. The families need one shared name table.
- **Run records have no token counts:** the cost model can't be measured until they do (OEE pt 10).
