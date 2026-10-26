# build-L1-bus HANDOFF · gen-4 -> gen-5 · 2026-10-26T02:4xZ (routing RECYCLE 02:17Z, compacted 1x)
Read first: status/build-L1-bus.card.md (rules + D-ids + item table, authoritative); l2-cards/phase-fix-r2/{replan-r3.md (design D1-D7, I1-I6'), fix-r4.md (r4 fix brief, done)}.
Parent routing (socket 487663, sends D-ids). Operator away till ~05:30Z: decisions -> routing.

## Mechanics
- hb: detached setsid loop (pid 3562149; ops/hb-keys.txt; every 30 min). Leases to 2026-10-26T07:16Z -> renew before (release + `claim --lease 12h`). No ops/hb.sh loops (D-2844), no cron.
- Tools /home/s/.local/share/plangraph/build-L1-bus-tools/: gate.sh (host gate, exit 0 admit; cap 4 runs, MemAvail >= 2000 MB, my budget 2), run-slice.sh, run-fix.sh S (brief l2-cards/phase-fix-r2/fix-$S.md; 45-min stall + 20-min first-write cancel, D-2865), mkr4.sh CTX (r4 seat prompt), r4wait.sh (glm admit + wait both seats), chain.log.
- Spawner from lane worktree: `PYTHONPATH=~/code/plangraph-worktrees/main-ro/src python3 -m plangraph.spawner.cli spawn|wait|status|result|cancel`; --leg needs --override-reason (>=10 chars); risk_tag public-contract (not "keeper"). Seat legs qodercli-qwen3.8-flash, opencode-glm-5.3-flash-free; writer families excluded: mimo,gemini,openai. Seat prompt < 128 KiB; answers <= 3500 chars (4 KB stdout tail).
- mimo-pro writer runs end "completed" WITHOUT committing (S5, S6, r4): L1 verifies diff vs brief + checks, commits with `Writer: mimo/mimo-v2.6-pro plangraph-spawner <run id>`.
- Tests: rust/worker `PG_JAIL=off PG_TEST_ALLOW_UNJAILED=1 PG_TEMPORAL_ADDR=http://127.0.0.1:17233 cargo test` (temporal `temporal server start-dev --headless --port 17233` via setsid if down). pytest -n 4. Scrub = CI form over tracked files only (worktree has target/): `git ls-files -z | tar ...` to a tmp dir, then `scrub_check.py <dir> --rules tools/split/manifest.toml --rules tools/split/scrub_ci.toml`.
- CI `gh workflow run tests.yml --ref lane/phase-fix-r2a`; verify push via git ls-remote. gh API rate limit hit once: few calls.
- Keeper refusal body is FLAT `{"error": code, "msg"}` (api.py); fleet bus uses nested.

## R2a' lane/phase-fix-r2a (~/code/plangraph-worktrees/bus-fix-r2a), base 59c1287e
S1-S6 green; 89da4c78 main merge (D-2876 Gate-Edit; SQLite SCHEMA_VERSION 16 = oversight 14 + bus 15/16; PG Migration 8 oversight, 9 bus); 91e47fec scrub allowances (23 per-value entries for the S6 fixture); 27948c8f D-2878 static bus STATUS + msg_rate_limited 429 (CI green 40252435122); bf7c3bfb r4 fixes (writer run-937de96f): park 409 -> KeeperError::Conflict(code), typed stale only for exec_stale_state; connect-deadline test; ERROR_STATUS parity test; PG order test renamed _bus_9; CHANGELOG phrase. L1 checks green (cargo 364, pytest 7686, scrub 0). CI triggered 02:4xZ on bf7c3bfb -> check it.
r4 seat evidence: build-L1-bus-seats/phase-r4-R2a-27948c8f/ (qwen = fix; glm failed free limit).

## Next
1. CI green on bf7c3bfb -> delta re-review: qwen + glm-5.3-flash-free (D-2876: no Haiku, no paid; glm limit -> wait reset). Prompt = r4 findings + triage (rejected items with evidence, no rulings D-302) + git diff 27948c8f..bf7c3bfb + touched functions; evidence dir build-L1-bus-seats/phase-r4b-R2a-bf7c3bfb/.
2. Clean -> `gh pr create --base phase/rt-p1-bus` + `gh pr merge --merge --match-head-commit <sha>`. Not clean -> fix brief per D-2888 (<=~4 findings or one function scope, "edit only <file>:<fn>" line, prior findings, replies <1500 tokens), mimo-pro.
3. R2b (wake + docs, I5-I9; replan-r3.md "R2b") mimo-pro, gated, 1 qwen seat, merge.
4. Merge main into phase once -> phase PR -> `GO-REQ phase/rt-p1-bus <head> <seat paths + sha256>` to routing. bus-discovery-adopt HOLD (D-2730).
5. After merge: verdicts + evidence on items (card table).
Stall rule D-2843: a slice stalling twice -> stop + report routing. D-2853 hand-edit cap 50, never AUTH_DOOR_PATHS.
QUIET: no cron, one exiting bg wait, card <= 40 lines, messages only BLOCKED/DONE/GO/incident/handoff.
