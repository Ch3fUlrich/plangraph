# build-L1 card · gen-18 · 2026-10-22 ~20:05Z · hb bg bxastgt52 · D-2270: server-L1-routing retired -> HIGH GO-REQ to routing; OC L2 env unset -> Claude Sonnet L2 fallback · HANDOFF docs/build-L1-handoff.md #19 · NO cron · hb bg loop 25m (15 keys ops/hb-keys.txt; restart FIRST)
Control repo LOCAL ONLY (D-1751): `git push control build-L1`. Writers D-2248 opencode free > mimo > deepseek; NEW L2s = OpenCode sol via workst-L1-autoos (D-2265); never Claude writers; D-2240 Hand-Edit own commit.
Seats D-2205 + D-2217 (family per commit writer; Claude may seat over non-Claude; 2 Claude=1 family; auth/live keep >=1 non-Claude). Seat prompts name head sha (D-2225). HIGH = 2 families + judge GO per exact sha, follow-ups too (D-2218). New leaf = JSON + gate edge to routing (D-2230).
glm/nvidia verdict reason must name record path + banner + sha256 review/err (D-2226): seat_verdict.py reason lacks them -> post glm via `PGTOK=reviewer-zhipu ops/pg.sh verdict` with full reason (LOWS glm went via tool: minor miss, disclose).
TOOLS: seat_verdict/tools run from detached checkout /home/s/code/plangraph-worktrees/sv-dry-1dbc4f26, refresh to each main (D-2234). Keeper LIVE runs FROM live-2d61e281 (cwd+PYTHONPATH): never ff it as tools; keeper code move = HIGH D-825 GO-REQ.
## Main = ef4cab7d (#119). Today merged: #94 #105 #106 #108 #110 #111 #107 #116 REDACT-LC 259dc54b, #113 PHASE S1 e9486140, #118 DEVSTACK (all DONE except PHASE-RUNNER).
## Lanes
| Item | Owner | State / next |
|---|---|---|
| RT-P1-EXEC-OWNERSHIP (HIGH) | exec-wfv | #112 31c525d8 CI green; r6 deepseek FIX (H1 park excludes waiting, H2 parked no owner clear, M3 retry 409) -> fix round sent; qwen PASS (3rd try). qodercli: big prompts via stdin + "You have NO tools" header line, NOT --attachment |
| RT-P1-PROJECTOR-RUN-IDS (HIGH) | - | D-2235 created; deps EXEC; claim after EXEC done |
| RT-P1-WF-VERSIONING | exec-wfv | held until EXEC merges; leg B workflow.rs redo by writer |
| RT-P1-PHASE-RUNNER (HIGH) | spine | S2 MERGED ef4cab7d (GO D-2291, head 02a5b0eb = 4c88ad17 + clean main merge); ciev bg bx1u58lc0 evidence only. S3: spine/phase-runner-s3a (keeper verb phase_finish, D-2227) + s3 (worker). HARDEN: add D-2288 criteria when open |
| RT-OVERSIGHT-CHECKS | DONE | 5fe070b3 verdict alibaba; timer = operator ask #3 at workst (needs operator OK); was MERGED 5fe070b3 (GO D-2274); ciwait bg b2ansobqb -> evidence+submit; then verdicts qwen/haiku seats/oversight-117d; timer install workst keeper only (D-1854) via workst-L1-autoos; LOW followup leaf -> routing |
| SEAT-COVERAGE-KEEPER (HIGH) | flow-oversight-2 | after oversight dispatch: red tests -> judge GO-REQ (GitAdapter trailer enum + quorum store_review._verdict) |
| CI-COMMIT-GATE-HARDEN | DONE | 1e1a5ec4, verdicts alibaba+zhipu (glm banner gap disclosed) |
| CI-COMMIT-GATE-EDGES (HIGH) | spawner | claimed by me 20:4xZ; edges (1)-(9), (8)(9) code+test; MERGE-READY -> GO-REQ routing |
| DEVSTACK-TEST-FLAKE | DONE | #118 7dd85d04, qwen seat |
| fleet-outbox-read | me | b330998 + harden writer run 20261022-160238; then test, seats glm+deepseek on head -> GO-REQ; install /home/s/bin + authorized_keys after GO; stage token COPIES, delete after pull |
| PROJECTOR-RUN-UNIT | DONE | qwen r2 PASS 59a5e0e5, verdict alibaba; PH-RT-RUNTIME 13/33 |
| workst L2s sandbox, card-checks, bus, oc-harden | sandbox c15, oc-harden c16, card-checks c17 LAUNCHED; bus HELD (RAM 84.4%, workst BLOCKED to routing) | admission clear -> oc-harden writer + sandbox seats, then bus |
| D-2212 BA-TOKENS | workst-L1-basic-analysis | await 'BA-TOKENS OK' -> routing |
| D-2254 low (after crit path) | me | PH-OPERATOR gate->GOAL holds (checked 19:2xZ); OS-149 A6 keeper cfg after prox A1-A5; RT-P4-ARCHIVE parked until cutover+1wk |
Parked (D-1932): UI-DEPLOY-AUTO set_kind, UI-BROWSER-TESTS, PG-REPUBLISH-FRESH, PG-PUBLIC-READY.
## Traps
- zsh: `"${H}:path"` not `$H:t`; no `h()`; `echo ===` fails; string vars don't split -> bash scripts (tmp/ciwait.sh SHA KEY = CI wait + evidence + submit).
- Never dump process environ / unit env values (DSN leak 10-22, D-2234 KEEP). Names only.
- Merge: `gh pr ready N` then `gh api -X PUT .../pulls/N/merge -f sha=HEAD -f commit_message="Plan-Item: plangraph:KEY"`.
- GH check-runs lag; DONE items are locked (no more evidence). Claim needs deps done.
- Never push site strings / seat prompts to GitHub (repo public). Private: ~/.local/share/plangraph/{seats,relay}.
- Never touch .claude/worktrees/proposal (frozen; render/proxy use it).

- oc-harden #95 d542cf0 BLOCKED: workst admission 82.6% commit charge; asked workst-L1-autoos to reap; fix = no_change migration PG6 + sqlite rebuild (cx writer).
- sandbox #100 fix: WA run 20261022-204430-...759ac4 (mimo, wt attempt-sandbox-wA) -> push lane/attempt-sandbox-wA; WB next (wt attempt-sandbox-wB, brief in transcript: git hardening attempt.rs + walk); WC = L2 on workst (sol). L2 cherry-picks + CI; I re-seat glm+deepseek.
- KEEPER-OPCLAIM-CLOSE (HIGH, D-2283/2284, mine): wt opclaim-close lane/keeper-opclaim-close; writer omniroute/mimo-v2.6-flash run 20261022-202358-...6d5fb8 (cx not declared on coding.vm, D-2148 skip). Design: A refuse legacy claimed on non-none tier, B legacy claim -> in_review recovery. Then PR, 2 seats non-xiaomi, GO-REQ routing, then router closes server:OP-DOCKHAND-EVENTS.
- sandbox #100 d0905ccb: seats glm+deepseek running on coding.vm (D-2287, seats/attempt-sandbox-100/r1, diff origin/main...head) -> GO-REQ routing. Admission order when free: sandbox, oc-harden, card-checks (one at a time).
- D-302 (D-2283): seat prompts = item+diff+artifacts only; disputes -> routing.
- workst admission (commit 84-90% >= 80%): oc-harden, sandbox, card-checks stalled; BLOCKED to routing *A cap 92% until 04Z.
- New leaves: RT-P1-CARD-CHECKS-KEEPER (HIGH, dep CARD-CHECKS, D-2289). exec-wfv CI 39848623437 green on 31c525d8.