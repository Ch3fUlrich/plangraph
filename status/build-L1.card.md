# build-L1 card · gen-18 end · 2026-10-23 ~05:45Z · HANDOFF docs/build-L1-handoff.md #20 · NO cron · hb bg loop 25m over ops/hb-keys.txt (restart FIRST)
Control repo LOCAL ONLY (D-1751): `git push control build-L1`. Writers: cx/gpt-6.1-sol not on coding.vm -> omniroute/mimo-v2.6-flash > deepseek (record skip). Never Claude writers.
Seats (D-2318/2322): qwen via qodercli (stdin, no-tools header) + opencode/glm-5.3-flash-free (opencode ZEN path, zhipu; opencode-go free = 429). FULL diff incl tests (D-2324). Prompt = done_criteria from `ops/pg.sh show` + diff, no rulings (D-302/2295).
HIGH = 2 non-writer-family PASS + routing GO per sha. glm verdict: `PGTOK=reviewer-zhipu ops/pg.sh verdict K --pass --reason "<record path, banner, sha256>"`; qwen: sv-dry-1dbc4f26 tools/seat_verdict.py.
SEAT-COVERAGE-KEEPER now ON MAIN: verdict refuses until every Writer family of leaf commits (merge-base..p2) is covered; Hand-Edit=anthropic; missing Writer = writer_unknown.
Spawn: MCP spawn BROKEN (deleted uv interpreter) -> CLI `python3 /home/s/code/AutoOS/tools/autoos-agent.py run --client opencode --model omniroute/mimo-v2.6-flash --card '{"complexity":"hard","privacy":"public","role":"implement"}' --isolate --title T "<task>"` (nohup &). Sandbox = history-less snapshot: commit conflicted merge as WIP first, rebuild merge via commit-tree. rc=7 LEAK on moved lane/seat-coverage-keeper refs = false positive.
Merge: ~/.local/share/plangraph/tools-l1/job-tmp/mergepath.sh PR HEAD KEY TITLE (handles main-already-in) / merge-cont.sh PR KEY TITLE after a manual CHANGELOG union. Evidence needs FULL 40-hex sha. Kind `commit` = router-only.
## Main = 9200bd7e (#129) ; f25cc546 (#125 S3c) ; f6b0053a (#128)
| Item | Owner | State / next |
|---|---|---|
| KEEPER-OPTASK-CLOSE #129 (HIGH) | flow-oversight-3 | GO D-2324; merge-cont running (merge-129 wt, CHANGELOG union) -> evidence -> submit -> verdicts qwen (seats/optask-129/qwen) + glm (seats/optask-129/r2/glmfree, sha 3b0a487a) |
| KEEPER-OPCLAIM-CLOSE #127 (HIGH) | L1 | fix writer 55a32e KILLED at timeout 05:24Z, uncommitted WIP saved ~/.local/share/plangraph/tools-l1/job-tmp/opclaim-r1-fix-wip.patch (REMOVED operator_task exemption = WRONG per D-2318: #127 KEEPS exemption, terminal-state test via OPTASK close path, merges AFTER #129). Next: new writer off #127 head 65c39584 + main (with #129): apply WIP minus exemption removal, keep deepseek r1 items 2-5; re-seat qwen+glm-free; GO-REQ; then legacy B server:OP-DOCKHAND-EVENTS |
| RT-P1-EXEC-OWNERSHIP #112 (HIGH) | L1 (exec-wfv offline) | head b9386bb9 pushed (W19+W20+W21 + main merge; resolve writer 4d57b6 keeper.rs; CHANGELOG union); rust 141 + worker 267 green; pytest full bg bq4vikono; CI dispatched -> re-seat qwen + glm-free FULL diff -> GO-REQ -> merge -> signal spine S3d |
| RT-P1-PHASE-RUNNER (HIGH) | spine (relaunched) | S3c MERGED f25cc546 (evidence on). S3d red spine/phase-runner-s3d @ 76904e43 (D-2326 lane reading, 3-way bypass guard, attach_merge_evidence after merge to main) -> impl after #112 signal. Then S4. HARDEN items 1-12 |
| RT-P1-ATTEMPT-SANDBOX #100 | build-L2-sandbox (workst) | owes WB merge + main + CI + MERGE-READY (stall 7.6h, pinged 05:1xZ); no reply -> give to sweep-runtime |
| #95 OPENCODE-HARDEN | build-L2-sweep-runtime | in progress; #71 CLOSED (D-2325) |
| #97 BATCH-PRECONDITION, #96 JEV-SHADOW | build-L2-sweep-keeper | in progress |
| PG-PUBLIC-READY / FLEET-SID-REFRESH / OCGO-FLEET-BUS | build-L2-close-inreview | PGPR: 67 gitleaks + 6 scrub, writer, each finding fixed/allowlisted + rotation answer before judge. SID: submitted (in_review); seats running -> L1 posts verdicts. OCGO: router re-scopes+closes (D-2327) |
| RT-P1-BUS-CONTRACT | build-L2-bus (workst, relaunched) | conditional GO impl (push red 28ba998 to origin first); MERGE-READY -> L1 |
| SEAT-COVERAGE-KEEPER, CI-COMMIT-GATE-EDGES | DONE | |
Briefs for sweep L2s: plangraph-worktrees/briefs/. Router plans 2nd lane (router-code + spawner moves, prio 8) after RT-P1-E2E green (D-2318 4). Reap timer -> RT-P1-E2E checklist (router edit).
## Traps
- zsh: use bash for `${H}:refs/...`; `echo ===` fails; pkill -f PATTERN kills own shell if pattern in cmd; `while pgrep -f X` matches itself.
- Never dump environ. Never push site strings/seat prompts to GitHub. Never touch .claude/worktrees/proposal. Keeper LIVE from live-2d61e281 (never ff).
- D-2315: no age-only deletes; clean only terminal-marker runs.
