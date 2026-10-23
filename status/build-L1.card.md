# build-L1 card · gen-20 · 2026-10-23 07:35Z · NO cron · hb bg loop b6h0tgz70 (25m x16) over ops/hb-keys.txt
Control repo LOCAL ONLY (D-1751): `git push control build-L1`. Writers: cx/gpt-6.1-sol not on coding.vm -> omniroute/mimo-v2.6-flash > deepseek (record skip). Never Claude writers.
Seats (D-2318/2322): qwen via qodercli (stdin, no-tools header) + opencode/glm-5.3-flash-free (opencode ZEN path, zhipu; opencode-go free = 429). FULL diff incl tests (D-2324). Prompt = done_criteria from `ops/pg.sh show` + diff, no rulings (D-302/2295).
HIGH = 2 non-writer-family PASS + routing GO per sha. glm verdict: `PGTOK=reviewer-zhipu ops/pg.sh verdict K --pass --reason "<record path, banner, sha256>"`; qwen: sv-dry-1dbc4f26 tools/seat_verdict.py.
SEAT-COVERAGE-KEEPER now ON MAIN: verdict refuses until every Writer family of leaf commits (merge-base..p2) is covered; Hand-Edit=anthropic; missing Writer = writer_unknown.
Spawn: MCP autoos-agent spawn WORKS again (gen-19, run 36d71a). CLI fallback: python3 /home/s/code/AutoOS/tools/autoos-agent.py run ... --isolate. Sandbox = history-less snapshot.
Merge: ~/.local/share/plangraph/tools-l1/job-tmp/mergepath.sh PR HEAD KEY TITLE (handles main-already-in) / merge-cont.sh PR KEY TITLE after a manual CHANGELOG union. Evidence needs FULL 40-hex sha. Kind `commit` = router-only.
## Main = 2cc0666a (#136) ; 827724f5 (#127) ; 4fb18030 (#137). LIVE keeper = live-2d61e281 (LIVE-SYNC-8 rolled back D-2345)
| Item | Owner | State / next |
|---|---|---|
| KEEPER-OPTASK-CLOSE | DONE | evidence b79eea55 (#132), verdicts qwen+glm 2/2 |
| KEEPER-OPCLAIM-CLOSE #127 | L1 | DONE 827724f5 2/2. LIVE-SYNC-8 ROLLED BACK (D-2345): 827724f5 store_tokens F2 strict item_key refused all new run tokens (AutoOS spawner writes no item_key). Live = live-2d61e281 PID 3040646. Fix-forward: AutoOS spawner records item_key (workst-L1-autoos lane) OR keeper title fallback; LIVE-SYNC-9 only after merge + mint smoke. OP-DOCKHAND waits on it |
| RT-P1-EXEC-OWNERSHIP #133 (HIGH) | L1 | fix writer 20261023-055023-exec-sq-fix1-43adad (sandbox fleet/sandboxes/exec-own-sq/..43adad, brief seats/exec-ownership/sq1/FIX-BRIEF.md) -> take commits onto wt exec-own-sq, squash w/ ONE trailer block (commit-tree) -> CI -> seats qwen + deepseek full diff -> GO-REQ (ETA told routing 08:30Z) -> merge, close #112 |
| RT-P1-PHASE-RUNNER (HIGH) | spine STOPPED D-2334 (card plangraph-worktrees/spine-card.md); L2-LAUNCH to routing when #133 merged for S3d impl | S3c MERGED f25cc546 (evidence on). S3d red spine/phase-runner-s3d @ 76904e43 (D-2326 lane reading, 3-way bypass guard, attach_merge_evidence after merge to main) -> impl after #112 signal. Then S4. HARDEN items 1-12 |
| RT-P1-ATTEMPT-SANDBOX #100 | build-L2-sandbox (workst) | owes WB merge + main + CI + MERGE-READY (stall 7.6h, pinged 05:1xZ); no reply -> give to sweep-runtime |
| #137 OPENCODE-HARDEN | DONE | merged 4fb18030, 2/2 (notes cite D-2339 + RT95 leaf); #95 closed |
| #96 JEV-SHADOW (HIGH) | sweep-keeper | head ba3f0600: qwen PASS, glm FIX+TG yes (no prod caller for QC/outcome keeper path). L1 ruling A: writer wires prod call sites + tests via prod entry, re-seat |
| KEEPER-RUNTOKEN-CLI-RUNS (HIGH) | L1 | D-2338 created (gate PH-RT-ADAPTERS); after phase-1 items |
| PG-PUBLIC-READY | L1 | D-2345 opt B: deepseek seat (gateway deepseek-flash) on r2 full diff -> seats/pr136/r3; then post verdict. #136 MERGED cbf21431 (CI green). evidence refused (in_review); qwen verdict 1/2 on OLD evidence 115379e8 (writer alibaba) -> glm refused coverage. BLOCKED A sent: router withdraw_review -> L1 evidence cbf21431 -> submit -> repost qwen+glm from seats/pr136/r2. Flip gated on history cut + gitleaks 0 (close-inreview) |
| RT-P1-BUS-CONTRACT | build-L2-bus (workst, relaunched) | conditional GO impl (push red 28ba998 to origin first); MERGE-READY -> L1 |
Briefs for sweep L2s: plangraph-worktrees/briefs/. Router plans 2nd lane (router-code + spawner moves, prio 8) after RT-P1-E2E green (D-2318 4). Reap timer -> RT-P1-E2E checklist (router edit).
## Traps
- zsh: use bash for `${H}:refs/...`; `echo ===` fails; pkill -f PATTERN kills own shell if pattern in cmd; `while pgrep -f X` matches itself.
- Never dump environ. Never push site strings/seat prompts to GitHub. Never touch .claude/worktrees/proposal. Keeper LIVE from live-2d61e281 (never ff; LIVE-SYNC-9 = HIGH GO-REQ after item_key fix + mint smoke).
- D-2315: no age-only deletes; clean only terminal-marker runs.
- TRAILERS (gen-19): keeper reads %(trailers) = LAST paragraph, last Writer wins. Any merge-candidate commit: ONE final trailer block (Plan-Item, all Writer, Test-Removal, Co-Authored-By, no blank line). #133 squash 8fd739b7 has a split block -> rebuild message (commit-tree) before merge. #134 told to reword. Open: OPTASK verdict passed although leaf f6fadc23 trailers show no Writer/Hand-Edit (split block) -> coverage maybe not firing; raise with routing at next GO-REQ.
| KEEPER-TRAILER-MULTI (HIGH) | L1 | PR #139 head 7676dedf CI 39919509830. r1: glm PASS, qwen FIX (MED %b reads prose lines as trailers). Fix brief seats/trailer-multi/FIX1-BRIEF.md -> spawn on next free slot (before PG-PUBLIC-FIX); then r2 qwen+glm -> GO-REQ |
| #100 SANDBOX | sweep-runtime | head 988665b4 CI 39923857991; r3 qwen PASS, glm FIX (HIGH escape.rs CI lane, MED env contract) disputed -> routing BLOCKED; L2 runs glm r4 (full artifacts) + deepseek gateway seat; merge on qwen + one PASS |
| RT95-EMPTY-RUN-FIXTURE | L1 | claimed; #137 merged -> spawn writer to capture a real opencode empty run when MemAvailable > 5 GB (D-2332; was 4.3 GB) |
| KEEPER-LEGACY-HUMAN-FAMILY (low) | L1 queue | D-2348: keeper rule legacy/operator claimant -> family 'human' (+test: non-legacy unknown still refused); bundle in keeper batch riding LIVE-SYNC-9 after SPAWNER-ITEM-KEY; then router re-posts OP-DOCKHAND deepseek 438f9e |
| PG-PUBLIC-READY r3 | L1 | deepseek FAIL verdict POSTED (D-2349, item back to claimed). Re-seat on PG-PUBLIC-FIX merge. Gate PG-REPUBLISH-FRESH deps [READY, FIX] |
| PG-PUBLIC-FIX (HIGH prio 8) | L1 | claimed epoch 1; wt plangraph-worktrees/pg-public-fix (lane/pg-public-fix @ cbf21431); brief seats/pg-public-fix/WRITER-BRIEF.md; SPAWN HELD: host 5 live runs > cap 3 -> spawn model omniroute/mimo-v2.6-flash when slot frees (cx skip: not on coding.vm). Seats qwen+glm+deepseek |
