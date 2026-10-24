# L2 brief: build-L2-qc-accepts (OpenCode, cx/gpt-6.1-sol, workstation)
Parent: build-L1-bus (took over from build-L1, D-2534). Load skill unattended-orchestration (L2 rules). Effort medium. Level L2: never merge/push main, no crons,
card <= 40 lines next to this brief, RECYCLE-READY <card> before 270k.
## Chunk (D-2532 P1, prio 1, HIGH; claimed by build-L1-bus, it heartbeats)
plangraph:RT-QC-ACCEPTS - read `pg show plangraph:RT-QC-ACCEPTS` (done criteria = spec) + check table routing docs/plan/qc-checks.md
rows Q1-Q5, Q9, Q10, Q12, Q15, Q17 (copy below). Split into lanes off origin/phase/rt-p1-bus, one per coherent group, e.g.
 A keeper code-accept Q1-Q4 (verify before any cap/judge record) + Q5 verdict (family, witnessed model, non-empty);
 B CI: Q9 interface parity worker->keeper verbs, Q10 design-rule tests; C Q12 secret-free durable logs (gate tail capped+redacted);
 D Q15 public-hygiene CI job (gitleaks all branches + full history, author emails, session trailers, PR refs) - coordinate with
   #162 PG-PUBLIC-READY .gitleaks.toml (L1 owns #162; do not edit .gitleaks.toml, consume it); E Q17 phase status only from keeper.
One test per check, red first. Workflow (.github) edits need a Gate-Edit D-n: send GO-REQ to build-L1-bus before writing them.
Q1 lane record = origin: head == origin/lane, tree, parent chain, merge-base on main. Q2 changed file list/count == record; > cap needs `atomic:` reason.
Q3 added lines carry no URL/IP/socket/requests and no key/token printing. Q4 rebased lane: seats carry over only if diff -U0 +/- lines identical, else re-seat.
Q5 seat counts only if family differs from writer and other seat, model witnessed, answer non-empty. Q9 every endpoint/verb a caller uses exists in callee.
Q10 each design rule has a test. Q12 secrets never enter durable logs/history. Q15 see D. Q17 phase/status numbers from keeper, never session claims.

## Phase-batch rule (OPERATOR D-2534, DECISIONS 3.0) - replaces per-task seats
- Every lane branches off origin/phase/rt-p1-bus (NOT main) and opens its PR INTO phase/rt-p1-bus.
- NO per-task seats. Lane is done when its PR CI is green: send `PHASE-READY <KEY> PR#<n> head <sha> CI <run ids>` to build-L1-bus; build-L1-bus merges into the phase branch.
- After all phase tasks land, build-L1-bus runs ONE HIGH review of the whole phase diff vs main; the findings come back to you as ONE fixer brief (D-2458).
- Writer brief carries the done criteria + attack vectors (D-2472); red tests first; pattern sweep.
- Writers cx/gpt-6.1-sol via autoos-agent spawn on the workstation; fallback mimo/mimo-v2.6-flash exact id (never omniroute/mimo), record skip+reason. Never Claude writers.
- Commit trailer: Plan-Item: plangraph:<KEY> + Writer: <model> <run id>. CI per D-2418; never coding.vm agent runs (D-437).

## Return (to build-L1-bus, SendMessage, one line each)
PHASE-READY <KEY> PR#<n> head <sha> CI <ids>.  BLOCKED <KEY> | what | options, *default.  Refusals verbatim, stop.
DONE note: commits, what remains, evidence per claim. Keeper: build-L1-bus holds claims + heartbeats; you never claim/submit.
When done: report, then stop this session.
