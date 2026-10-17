# L2 brief: build-L2-p1-rehearsal (NON-Claude L2 on opencode go; D-1851 budget mode, D-1533)
Owner L1: build-L1 (msgs via workst-L1-autoos bridge or build-L1 by name). Skill: unattended-orchestration (rules R-orch-13/14; l2-rules-common.md).
Host: workstation (WSL2). D-437/D-1554: any leaf on coding.vm is tool-less only. No Claude writers/seats (D-1851). Writers+seats: D-1704/D-1714 order,
seats differ in family from writer and each other; go first, opencode free only as gateway fallback (D-1840). Never push main, never merge, no crons,
status card <= 40 lines at status/build-L2-p1-rehearsal.card.md on your lane, RECYCLE-READY before context cap. No live steps (cloud.vm Temporal = GO via L1).

## Goal
P1 (plangraph:PH-RT-RUNTIME) is gated on live Temporal (server:RT-P1-TEMPORAL-CHECK, prox side). All -CODE items are done on main 539ef09d.
Rehearse P1 end to end on a LOCAL Temporal dev server so the live run only swaps the endpoint, and prove the architecture runs without Claude.
Design: /home/s/code/routing-worktrees/rethink-setup/docs/reports/2026-10-15-rethink-setup-design.md sections 4, 11 (P1 exit). Lane done-criteria:
`keeper show plangraph:RT-P1-{WORKER,PROJECTOR,INBOX,REVIEW,E2E}` (read-only; do not claim — L1 holds claims).

## Tasks (lane/p1-rehearsal from main 539ef09d; one PR per task, MERGE-READY with sha to L1)
R1 Audit: per lane, done-criteria clause -> file:line on main or GAP. One table, docs/plan/rethink/p1-rehearsal-audit.md. No code.
R2 Harness: script tools/p1_rehearsal.sh (+ README section) that starts `temporal server start-dev` (ephemeral), a scratch Postgres keeper
   (existing test fixtures/pg contract setup; never the live keeper), the Rust worker (rust/worker-core + worker bin), projector, inbox API.
R3 E2E on dev server: one synthetic card (trivial check, e.g. a doc edit in a scratch repo) passes readiness, runs on a free leg via Temporal + Rust
   worker, check passes, advisory-lock merge into the scratch repo, attempt + cost visible in the scratch keeper; one test question answered via the
   minimal Inbox resumes the run; reviewer activity returns a JSON verdict. Record commands + output in docs/reports/rethink-p1/rehearsal.md.
R4 Gaps found in R1/R3: fix as small lanes (failing test first), each its own PR.
R5 Drill plan (doc only, docs/plan/rethink/drill-ocgo.md): planner + judge on opencode go, worker card via the Rust worker, keeper + Temporal calls
   from BOTH hosts (coding.vm and workstation) against cloud.vm Temporal once live; steps, expected outputs, rollback, what needs a GO.
Order: R1 -> R2 -> R3 (R4 as found) ; R5 in parallel with R2. Max 3 concurrent writers.

## Return
DONE note: commits/PRs, per R-step the command + verbatim result, what remains, refusals verbatim, evidence for every claim; seat records (writer,
reviewer families, served==pinned, verdict).
