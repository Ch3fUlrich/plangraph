# L2 brief: build-L2-qc-accepts (OpenCode, cx/gpt-6.1-sol, workstation)
Parent: build-L1. Load skill unattended-orchestration (L2 rules). Effort medium. Level L2: never merge/push main, no crons,
card <= 40 lines next to this brief, RECYCLE-READY <card> before 270k.
## Chunk (D-2532 P1, prio 1, HIGH; claimed by build-L1, L1 heartbeats)
plangraph:RT-QC-ACCEPTS - read `pg show plangraph:RT-QC-ACCEPTS` (done criteria = spec) + check table routing docs/plan/qc-checks.md
rows Q1-Q5, Q9, Q10, Q12, Q15, Q17 (copy below). Split into lanes off origin/main, one per coherent group, e.g.
 A keeper code-accept Q1-Q4 (verify before any cap/judge record) + Q5 verdict (family, witnessed model, non-empty);
 B CI: Q9 interface parity worker->keeper verbs, Q10 design-rule tests; C Q12 secret-free durable logs (gate tail capped+redacted);
 D Q15 public-hygiene CI job (gitleaks all branches + full history, author emails, session trailers, PR refs) - coordinate with
   #162 PG-PUBLIC-READY .gitleaks.toml (L1 owns #162; do not edit .gitleaks.toml, consume it); E Q17 phase status only from keeper.
One test per check, red first. Workflow (.github) edits need a Gate-Edit D-n: send GO-REQ to L1 before writing them.
Q1 lane record = origin: head == origin/lane, tree, parent chain, merge-base on main. Q2 changed file list/count == record; > cap needs `atomic:` reason.
Q3 added lines carry no URL/IP/socket/requests and no key/token printing. Q4 rebased lane: seats carry over only if diff -U0 +/- lines identical, else re-seat.
Q5 seat counts only if family differs from writer and other seat, model witnessed, answer non-empty. Q9 every endpoint/verb a caller uses exists in callee.
Q10 each design rule has a test. Q12 secrets never enter durable logs/history. Q15 see D. Q17 phase/status numbers from keeper, never session claims.
## Writers / seats
Writers cx/gpt-6.1-sol via autoos-agent spawn; fallback mimo/mimo-v2.6-flash exact id (never omniroute/mimo), record skip+reason; never Claude.
Writer brief = criteria + attack vectors; one writer per round with ALL findings + pattern sweep. Trailer Plan-Item + Writer <model> <run id>.
Seats HIGH = 2, family != every writer family of the PR (D-2511): qwen qodercli > agy gemini > opencode free dsfree/glm; item+diff+artifacts only,
NO-tools header, no rulings (D-302); parallel to CI; delta re-review. Judge: L1 requests. CI per D-2418; no coding.vm agent runs (D-437).
## Return to build-L1
MERGE-READY RT-QC-ACCEPTS/<part> PR#<n> head <sha> CI <ids> seats <model+provider+family paths>. BLOCKED <item> | what | options, *default.
Refusals verbatim, stop. DONE note: commits, what remains, evidence per claim. When done: report, then stop this session.
