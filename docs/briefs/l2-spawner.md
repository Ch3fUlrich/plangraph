# L2 brief: build-L2-spawner (2026-10-22, D-2172 work order 3-5)
Parent: build-L1. Load skill unattended-orchestration. Sonnet, effort medium. NO crons, never merge/push to main, no live steps, card <= 40 lines, RECYCLE-READY before 230k.
Rules: docs/plan/rethink/briefs/l2-rules-common.md in the build-L1 worktree (/home/s/code/plangraph-worktrees/build-L1), binding: D-2148 writers, D-2166 seats, D-2046 Writer trailer, D-2038 spawner first.
Repo plangraph (PUBLIC: never site strings / seat prompts / private audit text). build-L1 holds every keeper claim (do not claim); ask build-L1 to claim the next item BEFORE its PR (keeper refuses evidence older than the claim).
Per item: read `pg show <key>` text first (build-L1 relays if you lack keeper access); worktree in /home/s/code/plangraph-worktrees/<lane>; merge origin/main in via merge commit, never rebase/squash.
Report to build-L1 only: MERGE-READY <PR> <head40> <CI run> <seat dirs + families> | BLOCKED <item> | what | options, *default | GO-REQ <what>.
Lanes in order (chain):
1. SPAWNER-NO-RUNTIME-IMPORT: import-guard test in the default suite (red on a fixture importing spawner selection outside planner/routing.py, green on main);
   flag default off until SPAWNER-UTILITY-PROPS closes (test); docs/SPAWNER.md head names the one path; 2 seats.
2. SPAWNER-UTILITY-PROPS (HIGH): every spawner/ PR needs a router GO until it is done -> GO-REQ to build-L1 before opening each PR. Property tests RED FIRST
   (commit them red, record the run), then fixes; sensitivity sweep committed; Writer trailer; 2 seats + judge.
3. PG-ROUTE-FROM-OMNIROUTE-DB (HIGH): blocked on autoos:OMNI-BENCH-PREP + item 2; prepare fixture-instance tests only when deps are done; BLOCKED line otherwise.
