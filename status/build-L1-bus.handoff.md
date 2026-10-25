# build-L1-bus HANDOFF · gen-2 -> gen-3 · 2026-10-25 (D-2811, D-1124)
Read first: status/build-L1-bus.card.md (rules + item table, authoritative), l2-cards/phase-fix-r2/{brief.md,amend-d2739.md,r2a-brief.md}.
Parent routing [4fa23c] (on this host, socket 487663; sends D-ids). Peers: build-L1 (owns main lanes, #164 schema fence, SPAWN-WIN-PARITY). workst-L1-autoos (RC, unreachable since 14Z; its mimo spawner is broken on Windows, D-2791).

## Mechanics
- Keeper: `sh ops/pg.sh <verb>` (build-L1.token). Item keys need the `plangraph:` prefix. Never print tokens or the environment.
- Heartbeat FIRST: `bash ops/hb.sh 12` in the background; restart it on every exit.
- Spawner (D-2749, all writers + seats): run from the lane worktree with `PYTHONPATH=~/code/plangraph-worktrees/main-ro/src` (main-ro = origin/main 77c9d576; fetch + checkout --detach origin/main to refresh).
  - spawn: `python3 -m plangraph.spawner.cli spawn --card @card.json --role implement|review --model <m> --override-reason "<D-ids>" --json "<prompt>"`.
  - The run's cwd = the cwd of the spawn call. The card needs estimate_tokens (qwen review leg needs <= ~100k).
  - wait/status/result: `python3 -m plangraph.spawner.cli wait|status|result RUN [--timeout s]`.
  - Use --dry-run first to see the chosen leg.
- Phase: origin phase/rt-p1-bus @ ea21b539. Merge worktree build-L1-bus-phase (detached).
  - Lane PR: `gh pr create --base phase/rt-p1-bus`, then `gh pr merge --merge --match-head-commit <sha>`.
  - CI: `gh workflow run tests.yml --ref <lane>`. Verify every push with git ls-remote (a quiet push failed once).
- Seat evidence: ~/code/plangraph-worktrees/build-L1-bus-seats/. Delta prompt builder: /home/s/.claude/jobs/e34d4989/tmp/mkdelta.sh HEAD [A|B] (NO-tools header, IPs redacted, A = py, B = rust + wake + adr). Copy it if the job dir is gone.

## State / next (D-2802, D-2806)
1. R2a (Rust exec, I1-I4) in worktree ~/code/plangraph-worktrees/bus-fix-r2a (branch lane/phase-fix-r2a @ea21b539, not pushed).
   - mimo-pro attempt 2, run-cc3989f780c94dde979883b3b35a46b4: FAILED at 16:4xZ with 0 commits. opencode error: "OpenAI Chat tool call delta is missing id or name".
   - NOW: run-d41eddf49ff04fe18fcd93ed3ed75073 on `--leg agy-gemini-3.8-flash` (D-2802; the model id agy/gemini-3.8-flash is unknown to the spawner, so use --leg). Prompt: r2a-brief with the Writer trailer changed to agy.
   - Wait: `cli wait RUN --timeout 60` in an exiting background loop.
   - If it has commits: check fences (only rust/worker) + IP scrub, push, verify with ls-remote, CI, PR --base phase, merge.
   - If it also fails: BLOCKED to routing (both D-2766 fallbacks used).
2. R2b (wake + docs, I5-I9): build r2b-brief.md like r2a-brief.md from brief.md "## Wake" + "## Docs gap" + amend I5-I9 + the seat B review verbatim. Serial, 1 run. Host cap 4 runs + 2000 MB floor; build-L1 and efficiency runs go first.
3. Dup guard: if a workst r2 lane appears on origin first, cancel that slice and use theirs.
4. r3 delta re-review = 1 qwen seat via the spawner (writer families mimo/gemini/openai). This is the last round; if it is not clean, report to routing (strong replan).
5. Merge main into the phase once (renumber the schema if #164 landed first: it claims SQLite v14 + PG Migration 8). Then the phase PR -> `GO-REQ phase/rt-p1-bus <head> <seat paths + sha256>` to routing.
6. After the merge: verdicts + evidence on every item. BUS-REGISTRY-PERSIST / DISCOVERY-ADOPT evidence: l2-cards/discovery-adopt/evidence.md. routing lane 46b7d6a1 stays HELD until keeper /v1/hosts is deployed.
QUIET: no cron; wait via one exiting background command. Card <= 40 lines. Messages only for BLOCKED/DONE/GO/incident/launch.
