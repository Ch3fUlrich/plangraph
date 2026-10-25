# build-L1-bus HANDOFF · gen-3 -> gen-4 · 2026-10-25T23:5xZ (routing RECYCLE, compaction-restart rule)
Read first: status/build-L1-bus.card.md (rules + D-ids + item table, authoritative); l2-cards/phase-fix-r2/{replan-r3.md (design D1-D7, invariants I1-I6'), replan-v2-slices.md (S1-S6 table)}.
Parent routing (socket 487663, sends D-ids). Peer build-L1 (main lanes, #164 schema fence, spawner gaps: stdout-tail-only result, argv-only prompt -> sent [d0f038]).

## Mechanics (changed since gen-2)
- Heartbeat: detached setsid loop already running (pid 3562149/3788468; every 30 min x20 over ops/hb-keys.txt; log /home/s/.claude/jobs/1c96c00f/tmp/hb.log). Leases 12h to 2026-10-26T07:16Z -> renew before then (release + `claim --lease 12h`; first beat must print ok). Do NOT start ops/hb.sh loops (D-2844).
- Tools (durable copy, private): /home/s/.local/share/plangraph/build-L1-bus-tools/ = gate.sh (D-2823 admission, exit 0 = admit), mkslice.sh, run-slice.sh (gate queue -> spawn mimo-pro -> 45-min no-change watchdog, D-2845), chain.sh FROM (serial slices + checks: commit or L1 auto-commit of a green diff, fence rust/worker, <=170 lines, scrub, cited-test grep, Writer trailer, push+ls-remote+CI), mkr3.sh (seat prompt builder <128 KiB). J defaults to that dir; the old job dir may vanish.
- Spawner: from lane worktree, `PYTHONPATH=~/code/plangraph-worktrees/main-ro/src python3 -m plangraph.spawner.cli spawn|wait|status|result|cancel`. Seats: `--role review --leg qodercli-qwen3.8-flash --no-fallthrough`, writer families gemini,mimo,openai excluded.
- Tests: rust/worker with `PG_JAIL=off PG_TEST_ALLOW_UNJAILED=1 PG_TEMPORAL_ADDR=http://127.0.0.1:17233` (temporal dev server up). CI `gh workflow run tests.yml --ref lane/phase-fix-r2a`.

## R2a' state (lane/phase-fix-r2a in ~/code/plangraph-worktrees/bus-fix-r2a, base 59c1287e = R2a agy)
| Slice | Commit | Notes |
|---|---|---|
| S1 D1a | 1d49fc99 | CI green |
| S2 D5 | d0590479 | writer diff, L1-committed; CI green |
| S3 D1b | b4f4110e | CI green |
| S4 D2-D4 | 4f5cf12f + a6d67585 Hand-Edit 3 lines | writer truncated, 230 lines; CI green |
| S5 D6+I3c | 35882dbf | run-92cf5c3a truncated; L1 checks green, committed unchanged (191 lines); CI triggered 23:31Z, check |
| S6 D7 fixtures | RUNNING run-19a5682293d74d069faaa5d874b64896 (mimo-pro) | started by gen-3 chain.sh 6 (bg job in gen-3 session; may die at stop). Fixture (b) = build-L1-bus-seats/r2a-prime-run2-partial/observe_park.json |
If the gen-3 chain died: `cli status run-19a5...`; still live -> `J=<tools dir> run-slice.sh S6 run-19a5...` re-watches it, then finish with `chain.sh 6`-style checks by hand; done -> verify commit/diff as chain.sh does. Stall twice -> stop + tell routing (D-2843); D-2853 allows a direct edit for a stalling slice (cap 50 lines, no writer live in the tree).

## Next
1. S6 green -> all slice CI green -> merge main into lane ONCE (renumber schema if #164 landed: SQLite v14 + PG Migration 8).
2. grep every cited test name (fn name) on the lane head.
3. r4 = 1 qwen seat over 59c1287e..head (mkr3.sh pattern, prompt <128 KiB, seat writes full review to a file; evidence dir build-L1-bus-seats/phase-r4-...). Include for the seat: S4 eprintln! logging in workflow code (LOW), S4 230 / S5 191 lines over 150 target. Not clean -> report to routing (D-2831).
4. Clean -> `gh pr create --base phase/rt-p1-bus` + `gh pr merge --merge --match-head-commit <sha>`.
5. R2b (wake + docs, I5-I9; scope replan-r3.md "R2b") on mimo-pro, 1 run, gated; 1 qwen seat; merge.
6. Merge main into phase once -> phase PR -> `GO-REQ phase/rt-p1-bus <head> <seat paths + sha256>` to routing. bus-discovery-adopt HOLD (D-2730).
7. After merge: verdicts + evidence on items (card table).
QUIET: no cron, one exiting bg wait, card <= 40 lines, messages only BLOCKED/DONE/GO/incident/launch.
