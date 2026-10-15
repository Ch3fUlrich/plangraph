# build-L1 handoff #1 — 2026-10-15 ~15:45Z (context cap)

Read first: brief `/home/s/code/routing-worktrees/rethink-setup/docs/plan/rethink/build-L1-brief.md`, design (binding, 14a)
`.../docs/reports/2026-10-15-rethink-setup-design.md`, card `status/build-L1.card.md` (this branch), then this file.
Control branch = `build-L1` (this worktree); never merged to main. Code lanes = Agent subagents (sonnet, isolation worktree,
they land under /home/s/code/plangraph/.claude/worktrees/agent-*), push lane/* branches; I PR + merge to main after CI + seats.

## Tools (this branch, ops/)
- `ops/pg.sh` keeper CLI wrapper; token PGTOK (default build-L1 = ~/.config/plangraph/build-L1.token). Copy to job tmp and fix PYTHONPATH if the worktree moves.
- `ops/seat.sh MODEL PROMPT OUTDIR TITLE` tool-less gateway seat (review-call.py, 300 s hard timeout -> split big prompts). `ops/assemble.py OUT part|label=path...` builds prompts.
- Working seat models: `ovh/gpt-oss-120b` (openai), `xiaomi-mimo/mimo-v2.6-flash` (xiaomi; timed out on a 55k prompt).
  NOT usable: `opencode/*-free` (403 "free tier only within OpenCode"), `meta_api/*` (401 no credentials).
- Reviewer tokens (D-1321/D-1327 GO): ~/.config/plangraph/reviewer-{openai,meta,xiaomi,deepseek}.token, by REVIEWING model family.
  D-1324 rule until contract fix is live: ok must be a JSON bool from the VERDICT line -> use tools/seat_verdict.py (lane below).
- Worktree isolation: compound shell commands with $VARS + git/python get refused; use plain commands or write a script file.

## Keeper rules learned
- commit+ci evidence needs: Plan-Item trailer `Plan-Item: <project:KEY>` on the commit, commit ancestor of origin/main, CI on that exact sha.
  Merge commits lack it -> put the trailer on a lane commit whose own push CI is green. Every writer brief must demand the trailer.
- Keeper config has NO "ci" block -> commit+ci never verifies. Approved in principle (D-1327c): add `"ci": {"plangraph": "Ch3fUlrich/plangraph"}`
  to ~/.local/share/plangraph/keeper.json, bundled into the contract-fix "GO <sha> ff+restart" request (exact diff + rollback).
- kind `commit` is router-only (POST /v1/add_evidence_router). tier none items close by router (`close` on a decision batch).
- Batch replay before sending: ops/mkreplay.py (sqlite .backup of the live DB copy + daemon on :18787, chmod 600 config), apply_batch.py with router token, delete copy after.

## Items (owner build-L1)
| Key | State | Next |
|---|---|---|
| plangraph:RT-P1-WORKER-CORE | claimed (lease ~17:14Z, heartbeat!) | merged main 2cd2c382 (PR#1); trailer commit fad33f3c on main via 575efd7a (PR#3), CI run 38759410266 green; evidence pending keeper ci config; then submit + record ovh PASS (docs/plan/rethink/seats/worker-core-ovh) via seat_verdict.py with reviewer-openai |
| plangraph:RT-P1-TEMPORAL-SPIKE | in_review (decision D-1321 + commit 3b4c51c3 verified) | tier none -> ask routing to close |
| plangraph:RT-P1-CONTRACT | claimed | lane/rt-p1-contract 0a3d6008 (154 black-box tests, 43/43 routes; fixes verdict bool bug + int 500->400). Seat ovh = fix, its HIGH is FALSE (/v1/whoami served via api_routes.py:32 READ_CALLS); MED: assert error msgs, keeper start timeout 20 s -> raise. Seat mimo split A/B running (bg; outputs /home/s/.claude/jobs/ee78d094/tmp/seat-ct/mimoA, mimoB). Then: small fix pass via the lane's writer, PR, merge, evidence, then "GO <sha> ff+restart" to routing with before/after (health + POST verdict "ok":"false" -> 400), keeper ci config diff, rollback sha d1b0b333 (current live) |
| seat_verdict tool | writer running (lane/rt-p1-seat-verdict, trailer RT-P1-REVIEW) | review + PR; send sha to fixes-L1 (they asked) |

## Other state
- server:RT-P1-PG-DEPLOY: server-L1-main planning (D-1305). Everything else in P1 waits on it (+ RT-P0-OR-DRIFT for CLASS-COMBOS, RT-P0-BASELINE for GOVERNOR).
- Disk (D-1319): Temporal SDK builds -> workstation (workst-L1-autoos); one CARGO_TARGET_DIR + cargo clean. D-1317: tool-less seats ok on coding.vm, max 4 fleet.
- Janitor change done (keeps check-passing dead attempts). WORKER lane must add: merge lock + fencing, Temporal glue (SDK 1.1.0, protoc, cancel-aware activities), keeper calls.
- Lane worktree /home/s/code/plangraph-worktrees/rt-evidence-1 (branch merged) can be removed.
