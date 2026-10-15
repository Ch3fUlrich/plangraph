# Writer brief: plangraph:RT-P1-KEEPER-PG-CODE, run W1 (Sonnet, build-L1)

Goal: the Python keeper gets a Postgres storage backend next to SQLite (default stays SQLite), proven by the frozen black-box
contract suite (tests/contract, RT-P1-CONTRACT) + the existing suite running against Postgres in GitHub Actions. HIGH lane.

## Setup / rules
- Worktree: `git -C /home/s/code/plangraph fetch origin && git -C /home/s/code/plangraph worktree add /home/s/code/plangraph-worktrees/keeper-pg -b lane/rt-p1-keeper-pg-code origin/main`. Work only there.
- NEVER touch /home/s/code/plangraph/.claude/worktrees/proposal (live keeper) or 127.0.0.1:8787. No docker, no local Postgres
  server, no systemd: Postgres exists ONLY as a GitHub Actions `services: postgres` container (D-1334). Locally: create a venv in
  the worktree (`python3 -m venv .venv-pg`, gitignored) and `pip install "psycopg[binary]"` for import/unit checks only.
- Iterate on Postgres through CI: push the lane branch, read `gh run list/view --repo Ch3fUlrich/plangraph --branch lane/rt-p1-keeper-pg-code`.
- Every commit message ENDS with ONE trailer paragraph (no blank line inside):
  `Plan-Item: plangraph:RT-P1-KEEPER-PG-CODE` / `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
  Check: `git log -1 --format='%(trailers:key=Plan-Item,valueonly)'`.
- Public repo hygiene: no hosts, IPs other than 127.0.0.1, home paths, user names, token-shaped strings
  (`python3 -m pytest -q tests/test_split_make_public.py` must pass). DSN only from config/env (`PG_DSN` / keeper.json `"dsn"`).

## Spec (design 3, 12, 14a: /home/s/code/routing-worktrees/rethink-setup/docs/reports/2026-10-15-rethink-setup-design.md)
1. Seam: one DB adapter in src/plangraph/keeper/db.py (connection, placeholder style, transactions, upsert/ignore, autoincrement,
   row factory, JSON, locking). Today SQLite uses BEGIN IMMEDIATE + single writer; on Postgres use SERIALIZABLE or explicit row/
   advisory locks so the contract suite's concurrency tests (one winner on concurrent claim, epoch/lease fencing) still hold.
   Keep SQL portable where possible; dialect branches only inside the adapter or small per-dialect SQL maps.
2. Schema: src/plangraph/keeper/schema.py gets a Postgres DDL path with versioned migrations (same logical schema).
3. Hash chain: event hashes must be byte-identical across backends for the same event sequence (canonical JSON, no
   backend-specific timestamp/float formatting). Add a test that replays the same ops on both and compares chain heads.
4. CI: .github/workflows job `pytest-postgres` with `services: postgres:16`, env PG_DSN, runs tests/contract + the keeper test
   modules that exercise storage. The existing SQLite pytest job stays unchanged and green.
5. W1 scope ENDS here (backend + parity). Not in W1: design-3 new tables/states (cards, attempts, decisions, questions/answers,
   budget; needs_input/waiting/failed/parked) and the SQLite->Postgres copy tool — those are W2/W3 briefs.

## Deliver
- First a short plan in docs/keeper-postgres.md (seam, dialect differences found, locking choice, migration approach), then TDD.
- Return <= 200 words: head sha, CI run ids (sqlite + postgres jobs) and results, open problems.
