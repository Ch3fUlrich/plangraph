# L2 brief: build-L2-rt-side-3 (successor of rt-side-2, gone ~2026-10-19)
Parent: build-L1. Load skill unattended-orchestration. Effort medium. NO crons, no merges/pushes to main, card <= 40 lines, RECYCLE-READY before 230k.
Repo: plangraph (PUBLIC: never site strings / seat prompts / private audit text). Keeper claims are held by build-L1 (do not claim).
Rules: D-1704 model order; D-2019 no Go writers/seats; D-2038 spawner first (autoos-agent spawn), model path+family from registry;
D-2046/2047: every NEW commit = non-Claude writer + `Writer: <served model> <run id>` trailer; existing commits are never redone;
removing a test needs `Test-Removal: D-NNNN`. Seat records name served model + provider + family.
Lanes (heads as of 2026-10-18; first `git fetch`, read each lane card/PR body, merge origin/main in via merge commit, never rebase/squash):
1. RT-P1-CI-GATE PR #90 (fa27ede8, lane/rt-p1-ci-gate): strict gate (missing Writer trailer or Claude writer = fail; merges ignored;
   Test-Removal trailer) + bootstrap tools/ci_commit_exempt.txt listing D-2047 shas of #87 #90 #94 #95 #96 (lists in build-L1 handoff #18).
   Mechanical reverts of authorship-only reverts (363a3e6). Tests per rule. 1 non-Claude seat. MERGE-READY first (others merge after it).
2. RT-P1-FLOW-UNBLOCK PR #87 (head 1dc1f4ed, lane/rt-p1-flow-unblock): ONLY restore test_claimed_item_without_any_event_alarms + loosened
   asserts (new spawner commit), CI, 2 seats different families. Then QC-ACCEPTS (plangraph:RT-QC-ACCEPTS) when FLOW-UNBLOCK done.
3. KEEPER-BATCH-PRECONDITION PR #97 (7c787302): finish docs/CLI dry-run, CI, seats (keeper engine = HIGH -> judge GO via build-L1).
4. RT-P1-EXEC-OWNERSHIP (lane/rt-p1-exec-ownership, A 0d03f9e B 00290c2, D1 Rust ExecId writer was running on mimo leg 6): finish D1, PR.
Report to build-L1 only: MERGE-READY <PR> <head40> <CI run> <seat dirs>, BLOCKED <item> | what | options, *default.
