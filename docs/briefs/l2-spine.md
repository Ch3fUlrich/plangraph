# L2 brief: build-L2-spine (2026-10-22, D-2172 work order 6-7; target RT-P1-E2E dogfood)
Parent: build-L1. Load skill unattended-orchestration. Sonnet, effort medium. NO crons, never merge/push to main, no live steps, card <= 40 lines, RECYCLE-READY before 230k.
Rules: docs/plan/rethink/briefs/l2-rules-common.md in the build-L1 worktree (/home/s/code/plangraph-worktrees/build-L1), binding: D-2148 writers, D-2166 seats, D-2046 Writer trailer, D-2038 spawner first.
Repo plangraph (PUBLIC: never site strings / seat prompts / private audit text). build-L1 holds every keeper claim (do not claim); ask build-L1 to claim the next item BEFORE its PR (keeper refuses evidence older than the claim).
Per item: read `pg show <key>` text first (build-L1 relays if you lack keeper access); worktree in /home/s/code/plangraph-worktrees/<lane>; merge origin/main in via merge commit, never rebase/squash.
Report to build-L1 only: MERGE-READY <PR> <head40> <CI run> <seat dirs + families> | BLOCKED <item> | what | options, *default | GO-REQ <what>.
Rust/Temporal builds: coding.vm RAM floor 1 GB (check MemAvailable before each build; one cargo job; cargo clean after); CI = GitHub-hosted (D-1380 local record if down).
Lanes in order:
1. RT-P1-WORKTREE-ROOT-DRILL: throwaway repo, pinned opencode version; relative + absolute writes from a worker attempt checked in worktree AND main checkout;
   commands + results recorded (file in the lane, path to build-L1 for the keeper); outcome reproduced / not reproduced.
2. RT-P1-PROJECTOR-RUN-UNIT: deploy/systemd plangraph-projector.service (Restart=on-failure) + install docs; the live checks (attempt reaches keeper in 2 polls,
   kill restarts it) are build-L1 live steps: hand over the exact commands.
3. RT-P1-ATTEMPT-SANDBOX (HIGH, after 1): FS write jail, per-attempt cgroup limits, WSL drive/interop off, attach leg refused unattended; jail helper shared with AGENT-JAIL; 2 seats + judge.
4. Then the held P1 lanes, brief docs/briefs/l2-predeploy-6.md items 1-4 (#94 GATE-WIRING after #90 merges, #95 HARDEN, #96 JEV, WF-VERSIONING),
   then l2-rt-side-3.md items 3-4 (#97 BATCH-PRECONDITION, EXEC-OWNERSHIP). Use the model rules above, not the D-1704/D-2019 lines in those briefs.
