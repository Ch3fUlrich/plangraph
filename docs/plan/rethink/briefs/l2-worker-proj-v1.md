# L2 brief: build-L2-worker-proj — plangraph:RT-P1-WORKER-PROJ-V1 (HIGH; workstation for Rust builds; parent build-L1)
Rules: /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/l2-rules-common.md (binding, incl. D-1380 local-CI fallback, PR-head trailer). TTL 4 h.
Done means (keeper item): rust/worker Card workflow follows docs/projector.md "Contract with the Card workflow (v1)" (main 66d87fe):
memo `plan_key`, attempt activity type `attempt`, `maximum_attempts=1`, activity result fields (`exit_class` required; harness, model,
combo, class, started_at, ended_at, tokens_in/out, cost, log_ref), workflow result `outcome`. A recorded worker history (rust/worker
recorded histories / e2e) projects via `plangraph.projector` (history.py + apply.py) with no unknown-shape alarm and expected attempt rows
— a CI test. 2 seats incl. non-Claude (gpt-oss + qwen via qodercli), HIGH seat format per vector file:function.
Known gap to resolve (decide + document, smallest change): worker outcomes include `cancelled`/`quarantined`, contract v1 lists
`done | parked | failed`. Either map in the worker or version the contract (v1.1) in docs/projector.md + history.py; state which and why.
Write set: rust/worker/** (workflow/activities result shape), src/plangraph/projector/history.py only if the contract is versioned,
docs/projector.md contract section, tests (Python projector test over a recorded Rust history fixture). Other keeper/store edits -> ask build-L1.
Worktree from origin/main, branch lane/rt-p1-worker-proj-v1; draft PR; head commit trailer `Plan-Item: plangraph:RT-P1-WORKER-PROJ-V1`;
CI on workst attempt-1 green (else D-1380 local record); MERGE-READY <sha> + CI run + seat dirs (Taildrop + sha256 to coding.vm) to build-L1.
Context: build-L2-worker-code (PR #13) and build-L2-projector (PR #16) built both sides in parallel; their PRs/DONE notes are the history.
