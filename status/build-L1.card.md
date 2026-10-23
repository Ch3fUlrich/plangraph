# build-L1 card · gen-20 · 2026-10-23 09:55Z/10:00Z · NO cron · hb bg loop b6h0tgz70 (25m x16) over ops/hb-keys.txt
Control repo LOCAL ONLY (D-1751): `git push control build-L1`. Writers: cx/gpt-6.1-sol not on coding.vm -> omniroute/mimo-v2.6-flash > deepseek (record skip). Never Claude writers.
Seats (D-2318/2322): qwen via qodercli (stdin, no-tools header) + opencode/glm-5.3-flash-free (opencode ZEN path, zhipu; opencode-go free = 429). FULL diff incl tests (D-2324). Prompt = done_criteria from `ops/pg.sh show` + diff, no rulings (D-302/2295).
HIGH = 2 non-writer-family PASS + routing GO per sha. glm verdict: `PGTOK=reviewer-zhipu ops/pg.sh verdict K --pass --reason "<record path, banner, sha256>"`; qwen: sv-dry-1dbc4f26 tools/seat_verdict.py.
SEAT-COVERAGE-KEEPER now ON MAIN: verdict refuses until every Writer family of leaf commits (merge-base..p2) is covered; Hand-Edit=anthropic; missing Writer = writer_unknown.
Spawn: MCP autoos-agent spawn WORKS again (gen-19, run 36d71a). CLI fallback: python3 /home/s/code/AutoOS/tools/autoos-agent.py run ... --isolate. Sandbox = history-less snapshot.
Merge: ~/.local/share/plangraph/tools-l1/job-tmp/mergepath.sh PR HEAD KEY TITLE (handles main-already-in) / merge-cont.sh PR KEY TITLE after a manual CHANGELOG union. Evidence needs FULL 40-hex sha. Kind `commit` = router-only.
## Main = 0f588e1d (#96 JEV-SHADOW) ; e50a9e91 (#142 MCP-UTF8) ; 94b9ef35 (#141). LIVE keeper = live-2d61e281 (LIVE-SYNC-8 rolled back D-2345)
| Item | Owner | State / next |
|---|---|---|
| KEEPER-OPTASK-CLOSE | DONE | evidence b79eea55 (#132), verdicts qwen+glm 2/2 |
| KEEPER-OPCLAIM-CLOSE #127 | L1 | DONE 827724f5 2/2. LIVE-SYNC-8 ROLLED BACK (D-2345): 827724f5 store_tokens F2 strict item_key refused all new run tokens (AutoOS spawner writes no item_key). Live = live-2d61e281 PID 3040646. Fix-forward: AutoOS spawner records item_key (workst-L1-autoos lane) OR keeper title fallback; LIVE-SYNC-9 only after merge + mint smoke. OP-DOCKHAND waits on it |
| RT-P1-EXEC-OWNERSHIP #140 (HIGH) | L1 | GO D-2386 on 9b9b0571 BUT schema conflict vs #96 (both PG Mig6/SQLite v13) -> merge3 writer 20261023-094746-exec-merge3-writer-d87d07 (exec=Mig7); then re-seat 2 fam (no qoder D-2389) + re-GO-REQ; follow-up EXEC-OBSERVED-STATE |
| RT-P1-PHASE-RUNNER (HIGH) | spine STOPPED D-2334 (card plangraph-worktrees/spine-card.md); L2-LAUNCH to routing when #133 merged for S3d impl | S3c MERGED f25cc546 (evidence on). S3d red spine/phase-runner-s3d @ 76904e43 (D-2326 lane reading, 3-way bypass guard, attach_merge_evidence after merge to main) -> impl after #112 signal. Then S4. HARDEN items 1-12 |
| RT-P1-ATTEMPT-SANDBOX | done | #141 94b9ef35 merged, evidence, verdict 2/2, DONE sent (Hand-Edit trailer deviation disclosed); follow-up RT-P1-SANDBOX-JAIL-E2E |
| #137 OPENCODE-HARDEN | DONE | merged 4fb18030, 2/2 (notes cite D-2339 + RT95 leaf); #95 closed |
| #96 JEV-SHADOW (HIGH) | L1 | merged 0f588e1d (Hand-Edit union WRITE_VERBS), evidence, in_review; qwen pass posted; glm fix (D-2381); D-2394: 3rd-family agy gemini seat r5 running -> verdict; leaf JEV-ATTEMPT-THREAD |
| KEEPER-RUNTOKEN-CLI-RUNS (HIGH) | L1 | D-2338 created (gate PH-RT-ADAPTERS); after phase-1 items |
| PG-PUBLIC-READY | L1 | D-2345 opt B: deepseek seat (gateway deepseek-flash) on r2 full diff -> seats/pr136/r3; then post verdict. #136 MERGED cbf21431 (CI green). evidence refused (in_review); qwen verdict 1/2 on OLD evidence 115379e8 (writer alibaba) -> glm refused coverage. BLOCKED A sent: router withdraw_review -> L1 evidence cbf21431 -> submit -> repost qwen+glm from seats/pr136/r2. Flip gated on history cut + gitleaks 0 (close-inreview) |
| RT-P1-BUS-CONTRACT | build-L2-bus (workst, relaunched) | conditional GO impl (push red 28ba998 to origin first); MERGE-READY -> L1 |
Briefs for sweep L2s: plangraph-worktrees/briefs/. Router plans 2nd lane (router-code + spawner moves, prio 8) after RT-P1-E2E green (D-2318 4). Reap timer -> RT-P1-E2E checklist (router edit).
| RT-OVERSIGHT-KEEPER-SCOPE #138 (HIGH) | build-L2-flow-oversight-3 [62798f] (adopted 09:5xZ) | head 458621be CI red; fix4 writer scope-fix4m; glm PASS; 2nd seat != qoder (D-2389) on final head -> expect MERGE-READY/GO-REQ |
| GOV-CTX-METRICS (p9) | L1 | claimed ep1; writer 20261023-092955-gov-ctx-metrics-writer-8928df (brief seats/gov-ctx-metrics); PG-BOOTSTRAP blocked on autoos:AO-OCL1-PW-FILE |
## Traps
- D-2389: qoder qwen 429 all accounts until ~10-24 15:30Z: seats skip qoder (record skip) -> opencode glm/deepseek free > OR free > agy gemini > mimo; no nemotron/kilocode
- zsh: use bash for `${H}:refs/...`; `echo ===` fails; pkill -f PATTERN kills own shell if pattern in cmd; `while pgrep -f X` matches itself.
- Never dump environ. Never push site strings/seat prompts to GitHub. Never touch .claude/worktrees/proposal. Keeper LIVE from live-2d61e281 (never ff; LIVE-SYNC-9 = HIGH GO-REQ after item_key fix + mint smoke).
- D-2315: no age-only deletes; clean only terminal-marker runs.
- TRAILERS (gen-19): keeper reads %(trailers) = LAST paragraph, last Writer wins. Any merge-candidate commit: ONE final trailer block (Plan-Item, all Writer, Test-Removal, Co-Authored-By, no blank line). #133 squash 8fd739b7 has a split block -> rebuild message (commit-tree) before merge. #134 told to reword. Open: OPTASK verdict passed although leaf f6fadc23 trailers show no Writer/Hand-Edit (split block) -> coverage maybe not firing; raise with routing at next GO-REQ.
| KEEPER-TRAILER-MULTI (HIGH) | L1 | fix1 9bb8c2 WIP-exited; taken + reworded c95fb135 (red a56c4013); full suite 4710 passed 22 skipped; pushed, CI 39929726634 -> r2 qwen+glm when local share < 3 -> GO-REQ |
| #100 SANDBOX | sweep-runtime | NOT MERGED: PR ci-gate red (run 39923865296: tests.yml hand-edit af92fc84 w/o Gate-Edit; 27076751 hand-edit >10 lines). L2 squash same tree -> lane/attempt-sandbox-sq on Gate-Edit D-id from routing (BLOCKED 09:1xZ). Seats r3/r4 valid by tree equality. Then mergepath + submit + verdicts |
| RT95-EMPTY-RUN-FIXTURE | L1 | claimed; #137 merged -> spawn writer to capture a real opencode empty run when MemAvailable > 5 GB (D-2332; was 4.3 GB) |
| KEEPER-LEGACY-HUMAN-FAMILY (low) | L1 queue | D-2348: keeper rule legacy/operator claimant -> family 'human' (+test: non-legacy unknown still refused); bundle in keeper batch riding LIVE-SYNC-9 after SPAWNER-ITEM-KEY; then router re-posts OP-DOCKHAND deepseek 438f9e |
| PG-PUBLIC-READY r3 | L1 | deepseek FAIL verdict POSTED (D-2349, item back to claimed). Re-seat on PG-PUBLIC-FIX merge. Gate PG-REPUBLISH-FRESH deps [READY, FIX] |
| PG-PUBLIC-FIX (HIGH prio 8) | L1 | writer 20261023-084625-pg-public-fix-writer-7bf42d running (wt pg-public-fix, brief seats/pg-public-fix/WRITER-BRIEF.md) -> PR, CI, seats qwen+glm+deepseek |
| PG-OMNIROUTE-CONFIG-HOME (prio 6) | L1 queue | D-2359: operator combos (names, members, order, strategy) = PUBLIC in plangraph, export via gateway API only (R-coord-15), scrub scan; keys/account ids/hosts stay private. >=2 tasks -> L2-LAUNCH (OpenCode sol) after PH-RT-RUNTIME items |
| UI-BROWSER-TESTS (prio 1) | build-L2-ui-browser (workst) | BLOCKED commit gate -> L1 ruled (a): L2 spawns cx writer for patch, Gate-Edit D-323 tests.yml, GO push lane/ui-browser-tests-r2, seats != openai |

| KEEPER-MCP-UTF8 | done | #142 e50a9e91, verdict done, Windows CONFIRMED by workst-L1-autoos |
| PG-BOOTSTRAP-FLEET-ENV (HIGH p9, secrets) | L1 queue | bootstrap.ps1+.sh fleet-env step, CSPRNG pw owner-only file + AUTOOS_OCL1_PW_FILE, never printed, --DryRun; dep autoos:AO-OCL1-PW-FILE; after MCP-UTF8 |
- Workstation 09:06Z: ui-browser L2 taskkill /IM python.exe killed MCPs/runs; briefs must say "kill only PIDs you started, never taskkill /IM". mergepath.sh: PR-event ci-gate check is authoritative, dispatch run skips commit-trailer gate.