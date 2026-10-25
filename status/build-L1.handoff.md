# build-L1 handoff (gen-29 -> gen-30, D-2706/D-2716 recycle, 2026-10-25 ~07:20Z)

Successor: read status/build-L1.card.md first, then this. FIRST restart the keeper heartbeat loop:
`cd ~/code/plangraph-worktrees/build-L1 && bash <jobdir>/tmp/hb.sh >> <jobdir>/tmp/hb.log 2>&1` (run_in_background; copy hb.sh from /home/s/.claude/jobs/975daff2/tmp/hb.sh; 3x8 min over ops/hb-keys.txt; restart on every exit).
Claimed keys (ops/hb-keys.txt): RT-OVERSIGHT-KEEPER-SCOPE (e4), TEST-SPEED-AFFECTED, RUN-EXIT-NOTIFY, PG-MODEL-ROUTE-HARDENING, ROUTER-LIVENESS-WATCHDOG, PG-L3-MAX-TOKENS, RT-P1-EXEC-OWNERSHIP-SQ12, LIVENESS-WATCHDOG-LOWS.

## Rules in force
- Writers D-2683: agy/gemini-3.8-flash > mimo/mimo-v2.6-pro > openrouter/free + opencode free > mimo-v2.6-flash; never Claude; cx out ~10-30.
- Seats: D-2540 first review 2 distinct families, re-review 1; D-2511 exclude every writer family; D-2539 Haiku first. Tool-less: Haiku `claude -p --model haiku --tools "" --strict-mcp-config --mcp-config tmp/nomcp.json` (stdin, empty cwd); qodercli `-p --model Qwen3.8-Flash --tools "" -o json`; opencode `run -m opencode/deepseek-v4.1-flash-free -f FILE "NO TOOLS..."`. Never agy -p (L24).
- D-2706: floor MemAvailable 2000 MB incl. projected RSS (qwen ~880 MB); max 4 writers here, rest -> workst-L1-autoos (3-writer cap). One compaction = recycle.
- Quiet: no cron; messages only BLOCKED/DONE/GO/incident.

## In flight at handoff (gen-29 bg shells die with the session; check each)
- Host writer cap (D-2716): routerctl saw 7 agent-run processes here. Start NO new writer on coding.vm until <=4; send writers to workst-L1-autoos.
- #184 (TEST-SPEED-AFFECTED, RUN-EXIT-NOTIFY, PG-MODEL-ROUTE-HARDENING): mergepath stopped on a merge conflict with main d85d38ff (#185). The lane moved spawner/fixer -> routing_support; #185 added settings thresholds + admit_review_scope (fixer.py, routing.py, runner.py, SPAWNER.md). agy writer hit 429 -> mimo-pro writer running (brief briefs/lanes-d2657/hardening-merge185.md, record seats/hardening-merge185.run.txt). Writers cannot push from the sandbox: fetch the sandbox branch printed under 'take it', ff/merge it into lane/d2657-hardening (worktree ~/code/plangraph-worktrees/d2657-hardening), run the brief's tests, push. If dead or failed: re-brief on workst. Then Haiku delta seat on the merge resolution (exclude google/xiaomi), green CI, mergepath 184 at the new head, evidence merge sha to TEST-SPEED-AFFECTED + RUN-EXIT-NOTIFY (`sh ops/pg.sh evidence <key> --kind commit+ci --commit <sha>`), submit 3, prune hb-keys. TRAP: a failed mergepath leaves ~/code/plangraph-worktrees/merge-<PR>; inspect it, then `git worktree remove` it before re-running.
- #187 PG-L3-MAX-TOKENS: lane/l3-max-tokens head f8a1b572 pushed (r2 ec7236e8 truncated_empty fix + r3 SEED_MODELS -> plangraph/catalog_data.py; guard untouched). Local 45 pass, gates 0. Next: PR #187 CI green on f8a1b572, Haiku delta seat (writer family xiaomi; prompt = item + r2 findings + diff f802fe96..f8a1b572), mergepath 187 plangraph:PG-L3-MAX-TOKENS, submit.
- #188 LIVENESS-WATCHDOG-LOWS: head f12532b0, mergepath running (log jobs/975daff2/tmp/mp188.log). On rc=0: `sh ops/pg.sh submit plangraph:LIVENESS-WATCHDOG-LOWS`, prune hb-keys; else re-run mergepath (check merge-188 worktree first).
- #185 REVIEW-METHOD-SETTING merged d85d38ff, submitted.

## Lanes
1. #164 RT-OVERSIGHT-KEEPER-SCOPE (HIGH), lane/keeper-scope-r6 head 88bc89d4 (r9 agy). r9 delta seats FAIL -> ROUND 3/3 brief scope-r10.md queued on workst (lane/keeper-scope-r10a group A store_oversight/auth/store, r10b group B db.py). Next: merge both into r6, keeper tests, delta seat (Haiku + opencode deepseek-free; writer families openai/xiaomi/qwen/google excluded), pass -> GO-REQ to routing naming round 3; fail -> replan with routing. Then new PR from r6 (close #164), mergepath. Schema v14 / PG Migration 8: tell build-L1-bus at GO.
2. RT-P1-EXEC-OWNERSHIP-SQ12 (HIGH, D-2703): brief exec-own-sq12.md, writer on workst (lane/exec-own-sq12). Next: 2 seats distinct families (not writer's, >=1 non-Claude), GO-REQ. Parent RT-P1-EXEC-OWNERSHIP stays in_review until SQ12 merges.
3. REVIEW-METHOD-SETTING-LOWS (routing-created leaf): claim, writer on workst.
4. #183 ROUTER-LIVENESS-WATCHDOG: merged 31331e80, routing 0e3f3045, timer liveness-watchdog@router ENABLED (drop-in -> pinned worktree live-liveness 31331e80, keep). Await routing close. Rollback `systemctl --user disable --now liveness-watchdog@router.timer`.
5. Serial keeper queue after #164: #166 KEEPER-PG-RECONNECT, #139 KEEPER-TRAILER-MULTI, KEEPER-WATCHDOG-ROLE (spec watchdog-principal.md).
6. Operator-gated: OS-PG-PUBLIC-PUSH (re-cut per pg-public-cut-plan.md on "push window now"; backups in ~/.local/share/plangraph/private/history-bundles), OS-KEEPER-LS9-MIGRATE (then z8 drop-in live-sync-z8-0a65abac.conf; live keeper 2d61e281).

## Traps
- zsh: wrap chains in bash -c. git cherry-pick has no -q.
- mergepath: KEY must be claimed; no submit.
- ci_commit_gate: one trailer block Plan-Item + Writer (non-Claude) or strict `Hand-Edit: <who> D-2240 <reason>` line (<=10 lines); gate paths need Gate-Edit: D-n.
- No `python` binary (test_planner_readiness smoke red locally); no xdist (serial, affected tests).
- ~/code/plangraph = control repo, not a plangraph checkout. Control push: git push control build-L1 only.
- Never dump environ; never push site strings/seat prompts.
