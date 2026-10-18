# L2 brief: build-L2-rtp1-predeploy-4 — successor of predeploy-3 (parent build-L1; TTL 6 h)
Rules: l2-rules-common.md (binding; sent with this brief). Sonnet, effort medium. HOST workstation.
FIRST read C:\fleet\handoff\build-L2-rtp1-predeploy-3.card.md (order, WIP heads, seat dirs). Stand predecessor down (R-orch-28).
Budget D-1919: writers qwen3.8-flash via qodercli first, then D-1704 order; seats non-Claude, 2 families that differ from the writer
(qwen writer -> seats gemini agy + mimo); you orchestrate only (<= ~10 lines yourself). Operator D-1932: only PH-RT-RUNTIME.
Never merge, never live steps, never write the live keeper. Context: delegate reading; keep your own context small (recycle at 230k).

Order (parallel where write sets differ):
1. plangraph:RT-P1-KEEPER-WORKER-VERBS (HIGH, claimed by build-L1, critical path): keeper serves /v1/attempts|park|complete|alarms|locks
   exactly as rust/worker/src/keeper.rs + projector call them; SQLite+PG parity; principal scope per verb; record_attempt re-report
   upserts (same key,n); attempt cost stored + shown by show/API; contract tests from the VerbFront fixtures, then VerbFront removed
   from tests/e2e; full local suite. Branch lane/rt-p1-keeper-worker-verbs, head trailer `Plan-Item: plangraph:RT-P1-KEEPER-WORKER-VERBS`.
   2 seats (one RUNS the tests, tool-using) -> MERGE-READY HIGH; build-L1 takes it to the judge.
2. DEVSTACK follow-up PR (trailer Plan-Item: plangraph:RT-P1-DEVSTACK): `docker rm -f -v` on --purge, KILL escalation after TERM on the
   health-timeout path, validate DEVSTACK_PG_MODE before temporal starts. Re-seat gemini + mimo on the final head; both must PASS.
3. Lane C RT-P1-E2E run script (WIP lane/rt-p1-e2e-devstack head 58902c7, e2e_devstack.py never run): against the REAL verbs from 1.
4. D plangraph:DEV-PGDRILL-LOCAL (+ devstack host-agnostic). 5. G gitleaks allowlist for sandbox_env.rs:312 test sentinel
   (Plan-Item: plangraph:PG-PUBLIC-READY). 6. P PG-PUBLIC-READY 2 seats (one runs gitleaks on all branches of a fresh mirror clone).
7. E plangraph-work branch triage table (superseded | to-port + reason) + ETA.
Per lane: MERGE-READY <pr> <head> + CI + seat dir tar sha256 to build-L1. Taildrop to coding-vm is unreliable: ALSO paste
small records (<= 4 KB) inline in the message. BLOCKED <item> | what | options, *default. RECYCLE-READY <card> before 230k.
