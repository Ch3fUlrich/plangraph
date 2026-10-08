# L2 brief: plangraph-L2-mk (MODEL-KINDS, 5 keeper lanes) — 2026-10-08, from plangraph-L1-main

You are a Sonnet L2 under plangraph-L1-main (D-716). Report ONLY to `plangraph-L1-main` (SendMessage).
TTL 4 h from launch. Budget: ≤ 10 seat calls; worker spawns only if routing has lifted the coding.vm spawn freeze
(memory floor); otherwise you write the code yourself (routing must have OK'd that for this L2 — the launch
message says so).

## Lanes (keeper items; `show` each with the MCP or read below)
Design (read first, it is the spec): /home/s/code/plangraph/.claude/worktrees/proposal/docs/proposals/2026-10-08-model-kinds.md
1. plangraph:MK-FIELDS (high) — layer/role/agent_id/phase_kind item fields; page roleOf uses role.
2. plangraph:MK-KINDS (high) — kinds question/decision/operator_task with operator/wait defaults.
3. plangraph:MK-FIXES-ERROR (high) — `fixes` field (max 2 per target) + `error` evidence kind.
4. plangraph:MK-ANSWER (high, after MK-KINDS) — router verb `answer`, event `answered`, final like closed_on_decision.
5. plangraph:MK-READS (standard, after MK-KINDS + MK-ANSWER) — read verbs needs_you, answers.
Do 1-3 first (independent), then 4, then 5. Stop after the TTL with whatever is done.

## Where / how
- Repo /home/s/code/plangraph. One worktree per lane from the CURRENT tip of origin/worktree-proposal (later lanes may
  base on an earlier lane branch when they depend on it):
  `git -C /home/s/code/plangraph worktree add /home/s/code/plangraph/.claude/worktrees/mk-<x> -b lane/mk-<x> origin/worktree-proposal`.
  Never touch `.claude/worktrees/proposal` (live services) and never delete any worktree.
- Pattern to copy: the `wait` field (commit 537d063, tests/test_keeper_wait_field.py) and `record_run` (store_runs.py,
  tests/test_keeper_record_run.py): migration `db._ADDED_COLUMNS`, `events.fold` + `verify_integrity`, store_create
  / store_input validation, importer `_FIELD_ORDER`, store_read show/snapshot, api_routes/mcp doors. A DB that never saw
  the new events must still verify clean; add a migration test (old items table → open → columns present).
- Files ≤ 400 lines (api.py, cli_verbs.py are at 400: do not grow them; route new verbs through api_routes tables).
- Tests: `python3 -m pytest -q tests` must stay at 0 failures (~5-15 min under memory pressure). Also
  `PYTHONPATH=src:tools python3 tools/split/make_public.py --source . --check --no-gitleaks` must stay ok (no LAN IPs,
  hostnames or high-entropy strings in src/tests).

## Seats (every lane; HIGH = 2 seats from 2 different non-anthropic families, standard = 1)
`cd /home/s/code/AutoOS && python3 tools/autoos_gateway_key.py exec -- python3 tools/review-call.py --model <m>
--max-tokens 32000 --prompt-file <goal + attack list + full diff> --out-dir <dir>`. Working models today:
`deepseek-v4.1-flash` (deepseek), `groq-qwen3.8-27b` (qwen), `ovh/gpt-oss-120b` (openai). End the prompt asking for
`VERDICT: PASS|FAIL`. Verify every claim yourself (most are false); fix real ones and re-seat. Record model, family,
verdict and sha256 of evidence.json per seat.

## Rules
- Push lane branches only. NO push/merge to main or worktree-proposal, no keeper writes, no service restarts.
- Stop and report on any permission denial; do not work around it.
- Never put plan data (data/, docs/plan files, keeper DB, snapshots) into a free-leg prompt; code diffs are fine.

## Report (≤ 30 lines, SendMessage plangraph-L1-main)
Per lane: status, branch + sha, writer, seats (model/family/verdict/evidence sha256), test counts, open points.
