# build-L1 handoff 2026-10-26 ~01:10Z (gen-35 -> gen-36, routing RECYCLE after 1 compaction)

Read first: status/build-L1.card.md. J = /home/s/.claude/jobs/0bbdff0b/tmp (scripts, briefs, seat dirs). Notes: status/notes-build-L1.md.
FIRST:
- hb loop runs detached ($J/hb.sh 90, log $J/hb.log; last ok 00:45Z). Restart only if stale >10 min. Keys: ops/hb-keys.txt.
- Live state: `PYTHONPATH=~/code/plangraph-worktrees/spawner-host-keys/src python3 -m plangraph.spawner.cli ps`; `gh pr list -R Ch3fUlrich/plangraph`. No runs of mine are live at handoff.
- Spawn only via `$J/gspawn2.sh <wt> <brief> <tag> <reason> <leg>` (host gate inside; budget 3). Fill the Writer RUNID by filter-branch over the writer commits only, before pushing.
- Hand-Edit cap is 50 lines (D-2853, #215 merged). Run the commit gate as `python3 tools/ci_commit_gate.py check --base $(git merge-base origin/main HEAD) --head HEAD` (without args it exits 2 = usage).
- Never use pkill/pgrep patterns that also match your own command line (killed my own shell twice). Kill by PID, or from a script file.
- gh pr edit is broken (Projects classic); use `gh api -X PATCH repos/Ch3fUlrich/plangraph/pulls/N -F body=@file`.

## Done this gen (all with full-sha commit+ci evidence on the item)
- #215 hecap50 -> 43aebc1d (D-2861, main CI 40235422603); ORCH-EFFICIENCY-ENFORCE.
- #216 S1 -> 7a101ae2 (D-2863, 40236390900); #210 closed -> #216.
- #217 admission renders gateway providers -> 8e87adf4 (D-2867, 40241534298).
- #218 async admission + admitted_by_render (D-2868/D-2869) -> 2bdde74a (D-2874, 40247596429).

## In flight
1. **#213 SPAWN-WIN-PARITY S3 (HIGH)**, branch lane/spawn-win-parity-s3, worktree spawn-win-parity-s3.
   - Fix r2 (mimo-pro run-d8d73ab9, RUNID filled): ATOMIC_WRITE_ATTEMPTS 24 (2.7 s), SYNC_SAVE_BUDGET_S 3.0, two-reads budget test.
   - Head ef200f3c: CI red, but only on tests/test_review_method.py::test_finding_7 (a result stub without **kw, from main #209 interplay). Fixed by L1 Hand-Edit 338b4f6a (1 line; gate rc 0; 96 passed locally). CI on 338b4f6aab5e955db608b92323928851d14c9a6a: run 40251267395 success (all jobs). GO-REQ sent 01:1xZ.
   - Delta seat Haiku (r2 delta 1ea63f65..ef200f3c): `fix` with Medium + 2 Low, all refuted by file:line in $J/seat-s3d2-haiku/refutation-L1.txt. Earlier refutation: $J/seat-s3d-haiku/refutation-L1.txt.
   - GO-REQ sent to routing (or send it if the card says pending). On GO: `gh api -X PUT repos/Ch3fUlrich/plangraph/pulls/213/merge -f sha=<full head> -f merge_method=merge`, wait for main CI, then `sh ops/pg.sh evidence plangraph:SPAWN-WIN-PARITY --kind commit+ci --commit <full merge sha> --run <run>` (template: $J/finish218.sh).
   - **After the merge:** tell workst-L1-autoos to run the one mimo-leg test now (the WinError 5 record write is fixed), then release the 4 Server lanes and the AO seat. Tell server-L1-main (ref [c46235], since several sessions share the name).
2. **S2** worktree spawn-win-parity-s2, branch lane/spawn-win-parity-s2, head b9e7094d (writer agy run-32b29199, RUNID filled). No PR yet.
   - 2 test_w29 failures after the main merge: test_opencode_harness_dispatch and test_unmocked_harness_popen[opencode-omniroute/cx-gpt-6.1-sol]. Likely the S2 stdin/prompt-argv change against main.
   - Main is now 2bdde74a (+#213 soon). Merge main again only if needed; fix via a mimo-pro round (findings verbatim) or a Hand-Edit if <=50 lines and fully specified.
   - Then PR, 2 seats whose families differ from every writer family of the PR (D-2511): Haiku + qwen if only google/xiaomi wrote; if L1 Hand-Edits, Haiku is out (use qwen + glm, or mimo-pro if mimo did not write). Then GO-REQ.
3. **REVIEW-METHOD-SETTING glm seat:** retry ONCE about 24 h after 20:15Z 10-25 (i.e. >= 20:15Z 10-26), or when an `opencode run -m opencode/glm-5.3-flash-free` smoke stops printing the limit. Command: `$J/glmseat.sh $J/seat-rms-glm rmsglm`. Post: `bash $J/postm.sh $J/seat-rms-glm plangraph:REVIEW-METHOD-SETTING "google --writer-family xiaomi --writer-family openai --writer-family anthropic"`.
4. **ORCH-EFFICIENCY-ENFORCE keeper seats at submit:** Haiku gets same_family (Hand-Edit fe7c4926), so a glm seat is needed after its reset as well.

## Queue order (after #213)
S2 > #211/#212 REVIEW-VERDICT-PARSE (#212 is no longer an open PR: verify merged/closed; rvpc owner = ask routing) > KEEPER-PG-RECONNECT S2 (brief $J/brief-pgr-s2.md, agy, worktree keeper-pg-reconnect-s2; live redeploy own GO) > spawn-iso #200/#201 (worktree spawn-iso-w) > PG-TEST-SHM-LEAK > SPAWN-PACKET-SCRUB.

## Gaps for routing / LESSONS
- Haiku seats emit "TEST-GAMING: no - reviewed/scanned ..." without "searched"; verdict.judge then treats it as missing (reported; fix belongs in the seat header).
- Every Claude Hand-Edit makes anthropic a writer family of the item, and Haiku seats are then refused (same_family). SPAWN-WIN-PARITY already has #218/#213 Hand-Edits.
- A shared session name (server-L1-main, build-L1-efficiency) needs a ref on SendMessage.
