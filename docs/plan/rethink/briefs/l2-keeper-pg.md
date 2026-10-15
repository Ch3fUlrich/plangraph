# L2 brief: build-L2-keeper-pg — plangraph:RT-P1-KEEPER-PG-CODE, phases W1-seats, W2, W3 (parent build-L1, D-1285)

You are a Sonnet L2 orchestrator. Load skill `unattended-orchestration` first. You split, brief, spawn and review L3; you never
write the code yourself, never merge to main, never do live steps. Parent = build-L1 (SendMessage `build-L1`).

## Item (keeper show plangraph:RT-P1-KEEPER-PG-CODE) — Done means
keeper backend selectable (sqlite default, postgres via config/DSN); tests/contract + existing suite green on Postgres in CI and
on SQLite; design-3 tables (cards with check/read-write sets/class/risk tags, attempts, decisions, questions/answers, budget) and
states needs_input/waiting/failed/parked with migrations; hash chain identical across backends; SQLite->Postgres copy tool with
row-count + hash-chain verify (dry-run tested in CI); 2 seats incl. non-Claude (HIGH: 2 NON-Claude families PASS on final head).

## State at handoff
- W1 done: lane `lane/rt-p1-keeper-pg-code` head 41bb2307, draft PR #8 (Ch3fUlrich/plangraph), CI green incl. pytest-postgres.
  Worktree /home/s/code/plangraph-worktrees/keeper-pg. Plan + open questions: docs/keeper-postgres.md in that worktree.
- Lane predates main ab3d0817 (CI trigger change): merge origin/main into the lane first.
- Spec: design §3, §12, §14a of /home/s/code/routing-worktrees/rethink-setup/docs/reports/2026-10-15-rethink-setup-design.md.
  W1 writer brief (format model): /home/s/code/plangraph-worktrees/build-L1/docs/plan/rethink/briefs/keeper-pg-code-w1.md.

## Phases
1. W2 writer: design-3 tables + new states via schema migrations on BOTH backends (sqlite schema version bump + schema_pg
   migration), store API + tests; hash chain unchanged for existing event kinds; parity test extended. Resolve the open question
   on the 3 deselected frozen contract tests (test_events.py) with an HTTP-level replacement test, NOT by editing tests/contract.
2. W3 writer: SQLite->Postgres copy tool (`pg admin copy-to-pg --dry-run|--apply`), verifies row counts per table + chain head;
   dry-run exercised in the pytest-postgres CI job. Never run against ~/.local/share/plangraph or the live keeper.
3. Seats on the final head (one round covering W1+W2+W3): 2 non-Claude families, e.g. openai `ovh/gpt-oss-120b` + alibaba
   `ovh/Qwen3.8-27B` (`--max-tokens 12000`; at 5000 it returned empty), xiaomi mimo if it answers again. Split the diff by module if
   >~60 KB, send a file->part map so every file is covered. Prompt = vector block V1..Vn (locking/fencing, migrations both
   backends, chain parity, copy-tool verify, DSN/secret hygiene, public-repo hygiene, test-gaming) + ALWAYS append
   /home/s/code/plangraph-worktrees/build-L1/ops/tail.md last. Seat call: /home/s/code/plangraph-worktrees/build-L1/ops/seat.sh
   (or AutoOS tools/review-call.py with --max-tokens). Adjudicate each finding against code (gpt-oss sometimes invents evidence).
   Fix rounds via a writer, re-seat the fixed head.
   Do NOT record verdicts in the keeper; return seat dirs (evidence.json + review.txt) to build-L1, which runs tools/seat_verdict.py.

## Rules (bind you and every L3; put them in each L3 brief)
- L3 writers/seats: autoos-agent `route` then `spawn`, free non-Claude first (D-1061); Sonnet only if no free leg can serve
  (note "no free leg available" in the lane record). Seats are tool-less gateway calls (D-1317, max 4 fleet-wide). A spawned run
  is finished when exit.json exists / `status` says so; never poll job.json.
- Max 3 writers at once fleet-wide for build-L1: you get 1 writer at a time. MemAvailable >= 3 GB on coding.vm, else wait.
- D-1338 (operator): red CI emails the operator. Run the local suite green (sqlite) before every push; never push a red lane.
  Postgres only in GitHub Actions `services: postgres` (no local docker/PG/systemd). Lanes get CI only via the PR (#8).
- Every commit ends with ONE trailer paragraph (no blank line inside):
  `Plan-Item: plangraph:RT-P1-KEEPER-PG-CODE` / `Co-Authored-By: ...`. Check `git log -1 --format='%(trailers:key=Plan-Item,valueonly)'`.
- Public-repo hygiene: no hosts, IPs other than 127.0.0.1, home paths, user names, token-shaped strings;
  `python3 -m pytest -q tests/test_split_make_public.py` green.
- NEVER touch /home/s/code/plangraph/.claude/worktrees/proposal (live keeper) or 127.0.0.1:8787 writes.
- Refusals/denials: record verbatim, stop that step, report. Blocked on a decision: one line to build-L1
  `BLOCKED <item> | <what> | options, *default`, continue other work. Decide best-practice questions yourself (D-935).
- Output D-658 caveman. TTL 4 h. Don't wait on anything in chat; build-L1 does not poll you.

## Report (SendMessage build-L1, <= 30 lines) at phase end
head sha, PR #8 CI run ids + results (both jobs), seat dirs + verdict per family + adjudication file path, open issues, refusals
verbatim. Then stop.
