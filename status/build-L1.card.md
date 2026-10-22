# build-L1 card · gen-17 end · 2026-10-22 ~19:45Z · HANDOFF docs/build-L1-handoff.md #19 · NO cron · hb bg loop 25m (15 keys ops/hb-keys.txt; restart FIRST)
Control repo LOCAL ONLY (D-1751): `git push control build-L1`. Writers D-2248 opencode free > mimo > deepseek; NEW L2s = OpenCode sol via workst-L1-autoos (D-2265); never Claude writers; D-2240 Hand-Edit own commit.
Seats D-2205 + D-2217 (family per commit writer; Claude may seat over non-Claude; 2 Claude=1 family; auth/live keep >=1 non-Claude). Seat prompts name head sha (D-2225). HIGH = 2 families + judge GO per exact sha, follow-ups too (D-2218). New leaf = JSON + gate edge to routing (D-2230).
glm/nvidia verdict reason must name record path + banner + sha256 review/err (D-2226): seat_verdict.py reason lacks them -> post glm via `PGTOK=reviewer-zhipu ops/pg.sh verdict` with full reason (LOWS glm went via tool: minor miss, disclose).
TOOLS: seat_verdict/tools run from detached checkout /home/s/code/plangraph-worktrees/sv-dry-1dbc4f26, refresh to each main (D-2234). Keeper LIVE runs FROM live-2d61e281 (cwd+PYTHONPATH): never ff it as tools; keeper code move = HIGH D-825 GO-REQ.
## Main = 7dd85d04 (#118). Today merged: #94 #105 #106 #108 #110 #111 #107 #116 REDACT-LC 259dc54b, #113 PHASE S1 e9486140, #118 DEVSTACK (all DONE except PHASE-RUNNER).
## Lanes
| Item | Owner | State / next |
|---|---|---|
| RT-P1-EXEC-OWNERSHIP (HIGH) | exec-wfv (workst) | PR #112 head 49a41892 r5: qwen PASS, deepseek FIX (F1 inbox doc bad_transition, F2 restore refused-park rollback test, F3 ADR migrate order, F4/F5 docs; seats/exec-ownership/r5). Writer round -> re-seat qwen+deepseek (tmp/mkexec5.sh, -f PROMPT.txt) -> GO-REQ |
| RT-P1-PROJECTOR-RUN-IDS (HIGH) | - | D-2235 created; deps EXEC; claim after EXEC done |
| RT-P1-WF-VERSIONING | exec-wfv | held until EXEC merges; leg B workflow.rs redo by writer |
| RT-P1-PHASE-RUNNER (HIGH) | spine | S1 merged; S2 #119 head 6a9c7d9c glm FIX 3 MED -> fix round -> re-seat -> GO-REQ; S3 own credential (D-2227); S4 |
| RT-OVERSIGHT-CHECKS | flow-oversight-2 | GO impl (standard): 2 writers cx-first, rebase main, 2 seats -> MERGE-READY |
| SEAT-COVERAGE-KEEPER (HIGH) | flow-oversight-2 | after oversight dispatch: red tests -> judge GO-REQ (GitAdapter trailer enum + quorum store_review._verdict) |
| CI-COMMIT-GATE-HARDEN | queue | input: def-line fixture change = false "test removal" (run 39817299743) -> match removed test NAMES |
| DEVSTACK-TEST-FLAKE | DONE | #118 7dd85d04, qwen seat |
| fleet-outbox-read | me | b330998 + harden writer run 20261022-160238; then test, seats glm+deepseek on head -> GO-REQ; install /home/s/bin + authorized_keys after GO; stage token COPIES, delete after pull |
| PROJECTOR-RUN-UNIT | in_review | re-seat after D-2190 |
| workst L2s sandbox, card-checks, bus, oc-harden | STOPPED (D-2251) | handoffs C:\fleet\handoff\build-L2-<name>.handoff.md; relaunch via L2-LAUNCH when lane comes up |
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
