# build-L1 handoff (D-2689 recycle, 2026-10-25 ~05:30Z)

Successor: read status/build-L1.card.md first, then this. FIRST restart the keeper heartbeat loop:
`cd ~/code/plangraph-worktrees/build-L1 && bash <jobdir>/tmp/hb.sh >> <jobdir>/tmp/hb.log 2>&1` (run_in_background; copy hb.sh from /home/s/.claude/jobs/6931599d/tmp/hb.sh; it reads ops/hb-keys.txt; restart on every exit and after every compaction).
Claimed keys (ops/hb-keys.txt): RT-OVERSIGHT-KEEPER-SCOPE (epoch 4), TEST-SPEED-AFFECTED, RUN-EXIT-NOTIFY, PG-MODEL-ROUTE-HARDENING, ROUTER-LIVENESS-WATCHDOG.

## Writers / seats in force
- D-2684: writers agy/gemini-3.8-flash > mimo/mimo-v2.6-pro > openrouter/free + opencode free (reset ~01Z) > mimo-flash last (<=3 files, commit early). cx quota out until ~2026-10-31. deepseek-direct-flash 402 (D-2680).
- Seats exclude every writer family of the PR (D-2511). Tool-less paths that work: qodercli `-p --model Qwen3.8-Flash --tools "" -o json` (stdin), opencode `run -m opencode/<m> -f FILE "header"`, Haiku `claude -p --model haiku --tools "" --strict-mcp-config --mcp-config <file {"mcpServers":{}}>` (stdin, run from an empty cwd). agy -p is NOT usable as a seat (LESSONS L24).
- Spawner: autoos-agent run --isolate --client opencode --model <m> --card '{"complexity":"standard","privacy":"public","role":"implement"}' (privacy private invalid; sensitive refuses non-registry models, never relabel, D-2682). agy writer: --client agy --model gemini-3.8-flash. A runner WIP commit has no trailer: re-commit with Plan-Item + Writer trailer.
- D-2686 memory: no new spawns on code host while MemAvailable <= 4000 MB; max 4 writers.

## Lanes
1. #164 RT-OVERSIGHT-KEEPER-SCOPE (HIGH). Branch lane/keeper-scope-r6 pushed, head = r8m pick of 26bbbb59 (mimo). Keeper tests 2706 pass, gates 0. Writers openai/xiaomi/qwen.
   - D-2688 seats over delta HEAD~1..HEAD (prompt briefs/lanes-d2657/seats/scope-r8.delta.txt): seat 1 Haiku DONE: VERDICT fail (all 12 earlier findings fixed/refuted-ok; NEW MEDIUMs: auth set_clock process-global clock, expired-token refusal writes no oversight_ops row vs docstring, db._upgrade drop+create guard window not atomic, + residual mint race N4) -> output seats/scope-r8.haiku.txt; NEXT: r9 fix brief with ALL Haiku new findings (writer per D-2684, <=3 files each), then delta re-seat (Haiku + opencode free at ~01Z); seat 2 deepseek-direct-flash is 402 -> opencode free glm or deepseek-v4.1-flash-free at ~01Z reset.
   - Next: on pass -> judge GO via routing -> new PR from lane/keeper-scope-r6 (old #164 is on lane/keeper-scope-repair: close it pointing to the new PR) -> mergepath plangraph:RT-OVERSIGHT-KEEPER-SCOPE. Schema: #164 keeps OVERSIGHT_SCHEMA_VERSION 14; build-L1-bus renumbers after.
   - On fail -> one fix brief with all findings (D-2458), keeper/schema multi-file = split <=3 files per writer.
2. #166 KEEPER-PG-RECONNECT: open, idle 18h, unclaimed. Serialized after #164 (same keeper files). Next: after #164 merges, claim, brief, writer per D-2684.
3. #139 re-cut KEEPER-TRAILER-MULTI: after #166.
4. KEEPER-WATCHDOG-ROLE (HIGH, router-created D-2687, deps #164, edge into GOAL-PG-FLEET): spec briefs/lanes-d2657/watchdog-principal.md. After #164. Coordinate schema version with build-L1-bus.
5. #183 ROUTER-LIVENESS-WATCHDOG (standard, D-2678/2682/2685): lane/liveness-watchdog pushed (worktree ~/code/plangraph-worktrees/liveness-watchdog), 5 files, 15 tests, gates 0. Writers mimo-flash + agy gemini. qwen seat DONE: VERDICT fail (seats/liveness.qwen.txt): HIGH 'not installed / no router relaunch config' = by design per D-2682/2685 (install + routing hand-edit after merge+GO) -> refute in brief; MEDIUMs real (misleading 'relaunch due but skipped' log on alert-only target; dry-run cannot reset episode state) + any LOWs -> r2 fix brief (agy gemini writer, files tools/liveness_watchdog.py + tests only), then delta re-seat qwen. Next: on pass -> MERGE-READY to routing with sha + CI -> mergepath 183 plangraph:ROUTER-LIVENESS-WATCHDOG. Ships alert-only. Then install liveness-watchdog@router.timer needs router GO; routing-side hand-edit <=10 lines (routerctl stamp write via `tools/liveness_watchdog.py stamp`, config naming relaunch cmd) in ~/code/routing-worktrees/router-liveness (lane/router-liveness, empty). Push cmd = env ROUTER_LIVENESS_PUSH_CMD (D-2681). Timers enabled only after OS-WATCHDOG-TOKEN for keeper writes.
6. #184 hardening (TEST-SPEED-AFFECTED, RUN-EXIT-NOTIFY, PG-MODEL-ROUTE-HARDENING; standard; head 7f1acb54, qwen seat): mergepath running at handoff (bg blp7i7xdo, key PG-MODEL-ROUTE-HARDENING). Next: when merged, attach commit+ci evidence (merge sha) to TEST-SPEED-AFFECTED and RUN-EXIT-NOTIFY (`sh ops/pg.sh evidence <key> --kind commit+ci --commit <sha>`), submit all 3, prune hb-keys. If the merge failed, re-run mergepath.

## PG public cut (D-2685)
- Backup: ~/.local/share/plangraph/private/history-bundles/plangraph-pre-cut-20261025T0500Z.bundle (--all, verified, 0600) sha256 8a30b4b46bc7400daaf0b722226499c27b6572224d727b9621f090b569363b46.
- Prepared cut: public-main 14930fff (1 commit, tree == main 3857ed0b, gitleaks clean), bundle plangraph-public-main-14930fff.bundle sha256 0de79029a27a84831d64d88bc479cb93c88a95a8152f631c00d94c409512b683.
- Operator steps queued OS-PG-PUBLIC-PUSH (rename/private/new public/push/protection = operator). When operator says "push window now": re-cut on then-current main per briefs/lanes-d2657/pg-public-cut-plan.md (gitleaks tree + scrub + orphan + tree-equal + gitleaks), report sha. Names scan waived D-2463.

## in_review idle >4h (verdicts are router-only)
- KG-LINK-REPAIR: #178 merged 4608fd3f, evidence ok, 0 reviews -> routing posts verdict (seat glm pass in briefs/lanes-d2657/seats).
- LESSONS-TO-ALGORITHM: #175 merged 0a65abac, evidence x2 -> routing verdict.
- RT-P1-EXEC-OWNERSHIP: 1 review (alibaba); needs 2nd family verdict -> routing (HIGH? check tier; seat files in pr171 lane notes).
- SEAT-VERDICT-QODER-ALIAS: #161 merged f8e5f051 -> routing verdict.
- KEEPER-OPERATOR-WAIT-GUARD: #158 merged 27082207 -> routing verdict; LOWs leaf KEEPER-WAIT-GUARD-LOWS.
- Also in_review now: KEEPER-SEAT-FAMILY-WRITER (#182 3857ed0b), PG-PUBLIC-READY (#177 ec69660a).
Action: send routing one line listing these for verdicts.

## LIVE-SYNC-9 (D-2664)
Live keeper stays 2d61e281. (a) smoke done. (b) OS-KEEPER-LS9-MIGRATE operator later today. (c) after (b): drop-in live-sync-z8-0a65abac.conf (worktree .claude/worktrees/live-0a65abac, keep), check /proc PYTHONPATH, health + snapshot count + md-sync tick; rollback rm z8. AutoOS live checkout already ff d386df2e (D-2673).

## L2s
All stopped except build-L2-review-method (d3c8d22a, D-2657 review-method lane; watch for MERGE-READY). build-L1-bus is a peer L1 (bus phase, schema v14/v15).

## Traps
- zsh word splitting: wrap chains in bash -c. Heredoc with $(...) is denied: quoted 'EOF' or python.
- mergepath: KEY must be claimed; it does not submit.
- Host has no `python` binary: tests/test_planner_readiness.py::test_smoke_checks_default_dry_run red on main here.
- No xdist on code host: pytest serial; affected tests only (D-2626).
- Never dump environ; never push site strings/seat prompts; control repo local only (git push control build-L1).
