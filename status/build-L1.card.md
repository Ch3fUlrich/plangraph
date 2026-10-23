# build-L1 card · gen-19 · 2026-10-23 ~06:00Z · HANDOFF docs/build-L1-handoff.md #20 · NO cron · hb bg loop 25m over ops/hb-keys.txt (bsqowsyzh)
Control repo LOCAL ONLY (D-1751): `git push control build-L1`. Writers: cx/gpt-6.1-sol not on coding.vm -> omniroute/mimo-v2.6-flash > deepseek (record skip). Never Claude writers.
Seats (D-2318/2322): qwen via qodercli (stdin, no-tools header) + opencode/glm-5.3-flash-free (opencode ZEN path, zhipu; opencode-go free = 429). FULL diff incl tests (D-2324). Prompt = done_criteria from `ops/pg.sh show` + diff, no rulings (D-302/2295).
HIGH = 2 non-writer-family PASS + routing GO per sha. glm verdict: `PGTOK=reviewer-zhipu ops/pg.sh verdict K --pass --reason "<record path, banner, sha256>"`; qwen: sv-dry-1dbc4f26 tools/seat_verdict.py.
SEAT-COVERAGE-KEEPER now ON MAIN: verdict refuses until every Writer family of leaf commits (merge-base..p2) is covered; Hand-Edit=anthropic; missing Writer = writer_unknown.
Spawn: MCP autoos-agent spawn WORKS again (gen-19, run 36d71a). CLI fallback: python3 /home/s/code/AutoOS/tools/autoos-agent.py run ... --isolate. Sandbox = history-less snapshot.
Merge: ~/.local/share/plangraph/tools-l1/job-tmp/mergepath.sh PR HEAD KEY TITLE (handles main-already-in) / merge-cont.sh PR KEY TITLE after a manual CHANGELOG union. Evidence needs FULL 40-hex sha. Kind `commit` = router-only.
## Main = 9200bd7e (#129) ; f25cc546 (#125 S3c) ; f6b0053a (#128)
| Item | Owner | State / next |
|---|---|---|
| KEEPER-OPTASK-CLOSE | DONE | evidence b79eea55 (#132), verdicts qwen+glm 2/2 |
| KEEPER-OPCLAIM-CLOSE #127 (HIGH) | L1 | GO D-2335 @01dbea7f; mergepath after #130 merge-cont (bg bwdtcb2bk) -> evidence/submit/verdicts qwen+glm r2 -> OP-DOCKHAND line: router `pg legacy server:OP-DOCKHAND-EVENTS in_review "<note>"` -> add_evidence_router decision -> deepseek seat verdict (D-2283) |
| RT-P1-EXEC-OWNERSHIP #133 (HIGH) | L1 | squash 8fd739b7 CI green; sq1 qwen PASS, glm FIX 3 MED (complete gate txn, run-less hand-back, PG behaviour test) -> fix writer 20261023-055023-exec-sq-fix1-43adad brief seats/exec-ownership/sq1/FIX-BRIEF.md -> re-seat qwen + deepseek (zhipu is a writer family: c129069f,f722147e) -> GO-REQ -> merge, close #112 |
| RT-P1-PHASE-RUNNER (HIGH) | spine STOPPED D-2334 (card plangraph-worktrees/spine-card.md); L2-LAUNCH to routing when #133 merged for S3d impl | S3c MERGED f25cc546 (evidence on). S3d red spine/phase-runner-s3d @ 76904e43 (D-2326 lane reading, 3-way bypass guard, attach_merge_evidence after merge to main) -> impl after #112 signal. Then S4. HARDEN items 1-12 |
| RT-P1-ATTEMPT-SANDBOX #100 | build-L2-sandbox (workst) | owes WB merge + main + CI + MERGE-READY (stall 7.6h, pinged 05:1xZ); no reply -> give to sweep-runtime |
| #137 OPENCODE-HARDEN (HIGH) | L1 | D-2339: squash fa7ea05e (tree == 9ad38dd2), mergepath queued (bg biz7r6lf2) -> evidence -> submit -> verdicts seats/95/r4 qwen+glm, note cites D-2339 + RT95-EMPTY-RUN-FIXTURE; close #95. Leaf JSON seats/rt95-empty-run-leaf.json sent to routing |
| #135 BATCH-PRECONDITION | DONE | merged 75a6f7ad, 2/2 |
| #96 JEV-SHADOW (HIGH) | sweep-keeper | head ba3f0600: qwen PASS, glm FIX+TG yes (no prod caller for QC/outcome keeper path). L1 ruling A: writer wires prod call sites + tests via prod entry, re-seat |
| KEEPER-RUNTOKEN-CLI-RUNS (HIGH) | L1 | D-2338 created (gate PH-RT-ADAPTERS); after phase-1 items |
| PG-PUBLIC-READY / FLEET-SID-REFRESH / OCGO-FLEET-BUS | build-L2-close-inreview | PGPR: 67 gitleaks + 6 scrub, writer, each finding fixed/allowlisted + rotation answer before judge. SID: submitted (in_review); seats running -> L1 posts verdicts. OCGO: router re-scopes+closes (D-2327) |
| RT-P1-BUS-CONTRACT | build-L2-bus (workst, relaunched) | conditional GO impl (push red 28ba998 to origin first); MERGE-READY -> L1 |
| SEAT-COVERAGE-KEEPER, CI-COMMIT-GATE-EDGES | DONE | |
Briefs for sweep L2s: plangraph-worktrees/briefs/. Router plans 2nd lane (router-code + spawner moves, prio 8) after RT-P1-E2E green (D-2318 4). Reap timer -> RT-P1-E2E checklist (router edit).
## Traps
- zsh: use bash for `${H}:refs/...`; `echo ===` fails; pkill -f PATTERN kills own shell if pattern in cmd; `while pgrep -f X` matches itself.
- Never dump environ. Never push site strings/seat prompts to GitHub. Never touch .claude/worktrees/proposal. Keeper LIVE from live-2d61e281 (never ff).
- D-2315: no age-only deletes; clean only terminal-marker runs.
- TRAILERS (gen-19): keeper reads %(trailers) = LAST paragraph, last Writer wins. Any merge-candidate commit: ONE final trailer block (Plan-Item, all Writer, Test-Removal, Co-Authored-By, no blank line). #133 squash 8fd739b7 has a split block -> rebuild message (commit-tree) before merge. #134 told to reword. Open: OPTASK verdict passed although leaf f6fadc23 trailers show no Writer/Hand-Edit (split block) -> coverage maybe not firing; raise with routing at next GO-REQ.
| KEEPER-TRAILER-MULTI (HIGH) | L1 | writer 20261023-064042-trailer-multi-writer-0e3d4e (wt trailer-multi off main 75a6f7ad) -> verify, PR, CI, seats qwen+glm (non-xiaomi) -> GO-REQ |
| #130 OVERSIGHT-FOLLOWUP | L1 | CHANGELOG union f02bc80e, merge-cont running -> evidence -> submit -> verdict qwen oversight-fu/r2 (writer deepseek) |
| #100 SANDBOX | sweep-runtime | handed over D-2332 |
| RT95-EMPTY-RUN-FIXTURE | L1 | D-2340 created+claimed (gate PH-RT-RUNTIME); after #137 merges: writer capture real empty run (needs opencode on host) |
