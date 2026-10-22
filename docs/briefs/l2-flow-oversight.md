# L2 brief: build-L2-flow-oversight (2026-10-22, D-2172 work order 1-2)
Parent: build-L1. Load skill unattended-orchestration. Sonnet, effort medium. NO crons, never merge/push to main, no live steps, card <= 40 lines, RECYCLE-READY before 230k.
Rules: docs/plan/rethink/briefs/l2-rules-common.md in the build-L1 worktree (/home/s/code/plangraph-worktrees/build-L1), binding: D-2148 writers, D-2166 seats, D-2046 Writer trailer, D-2038 spawner first.
Repo plangraph (PUBLIC: never site strings / seat prompts / private audit text). build-L1 holds every keeper claim (do not claim); ask build-L1 to claim the next item BEFORE its PR (keeper refuses evidence older than the claim).
Per item: read `pg show <key>` text first (build-L1 relays if you lack keeper access); worktree in /home/s/code/plangraph-worktrees/<lane>; merge origin/main in via merge commit, never rebase/squash.
Report to build-L1 only: MERGE-READY <PR> <head40> <CI run> <seat dirs + families> | BLOCKED <item> | what | options, *default | GO-REQ <what>.
Lanes in order:
1. RT-P1-CI-GATE PR #90 (lane/rt-p1-ci-gate fa27ede8): strict gate (missing Writer trailer or Claude writer = fail; merges ignored; Test-Removal trailer)
   + bootstrap tools/ci_commit_exempt.txt with the D-2047 sha lists (build-L1 handoff #18 "Exempt lists"). Tests per rule, 2 seats. MERGE-READY FIRST (#87 merges after).
2. RT-P1-FLOW-UNBLOCK PR #87 (lane/rt-p1-flow-unblock, head now eafb7695; read what changed since 1dc1f4ed): ONLY restore
   test_claimed_item_without_any_event_alarms + loosened asserts (D-2047), CI, 2 seats. Never rebase/squash.
3. RT-OVERSIGHT-CHECKS (prio 9, 6 h) + 4. RT-OVERSIGHT-KEEPER-SCOPE (HIGH auth: 2 seats + judge GO via build-L1): read done criteria; one writer per
   module; hermetic tests with a fixture keeper; no LLM in the checks path; live token only under a router GO (GO-REQ to build-L1).
