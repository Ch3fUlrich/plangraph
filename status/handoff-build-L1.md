# build-L1 handoff · gen-37 -> gen-38 · 2026-10-26 05:2xZ
Read: this file, status/build-L1.card.md, ops/hb-keys.txt, memory MEMORY.md. J=/home/s/.claude/jobs/0bbdff0b/tmp (scripts, briefs, seat dirs).
FIRST: hb loop ($J/hb.sh 90, check `pgrep -af hb.sh`; restart `nohup bash $J/hb.sh 90 &` if gone), claims in ops/hb-keys.txt, bg run r2b2.

## Done this gen
- LS9 DONE (D-2912): keeper live on a26573c3, schema 8, since 05:03:05Z (drop-in live-sync-z8-a26573c3.conf); 15-min watch clean; reported. Rollback A = remove z8 drop-in + daemon-reload + restart. Dump in backups/pre-go-live-sync-a26573c3-20261026T043853Z. Pending watch point: first seat_verdict post + first evidence commit+ci through the new code -> report failure to routing as incident.
- D-2918(A): $J/ls9-1b.sh / ls9-3.sh on postgres:17-alpine with tested --dry-run. (C): $J/spawnfix.sh moved to $J/retired-autoos/.
- #224 merged, evidence ok -> REVIEW-VERDICT-PARSE DONE. #222 PG-TEST-SHM-LEAK DONE.

## Open (priority order)
1. REVIEW-WITNESS-OPENCODE (HIGH prio 9, claimed, D-2921): PR #225 lane/review-witness-opencode head 001b4d90 (= #223 tree, retrailered; #223 closed). Seats r2 both FIX ($J/seat-ocw2-qwen, $J/seat-ocw2-haiku; vectors $J/ocw2.vec; diff $J/ocw2.diff). Findings to ONE mimo-pro fixer (<=4 findings, D-2888; writer worktree ~/code/plangraph-worktrees/opencode-witness on branch lane/review-witness-opencode):
   F1 HIGH: deny block via OPENCODE_CONFIG_CONTENT unproven vs repo/user config + MCP/plugin tools not in _DENIED_TOOLS. L1 checks: `opencode debug config` (v2.1.0) never lists the env layer (binary does load it as virtual doc "OPENCODE_CONFIG_CONTENT"); runtime test in $J/ocprec (project opencode.json allows bash, env denies) blocked by "Free usage limit reached" on glm-free 05:18Z -> rerun when the limit resets: `cd $J/ocprec; OPENCODE_CONFIG_CONTENT='{"permission":{"bash":"deny","edit":"deny","write":"deny","read":"deny"}}' opencode run --standalone --model opencode/glm-5.3-flash-free "Use your bash tool to run exactly: echo hi > pwned.txt ..."`; pwned.txt must not appear. Fix idea: also set OPENCODE_DISABLE_PROJECT_CONFIG=1 (+ OPENCODE_DISABLE_CLAUDE_CODE=1) in the review child env and deny MCP ("*" mcp) / add a test.
   F2 MED: sqlite connect without explicit timeout in _opencode_served_model (busy db).
   F3 LOW: pg-ocdata- temp dir leak on dry-run / render-refused / monitor cancel -> cleanup in finally + test.
   F4 LOW: test for V5 served-precedence (served set -> request-side ignored).
   --title verified: opencode 2.1.0 `run --help` lists --title. Then delta seats (qwen + Haiku OK: no Claude lines), judge GO-REQ per sha to routing, merge, evidence, post seats with key REVIEW-WITNESS-OPENCODE. Then $J/glmseat.sh copy request_side_model/witness_kind into evidence.json, re-run RMS glm seat -> post (REVIEW-METHOD-SETTING).
2. OPENCODE-ORCHESTRATOR-FALLBACK (claimed): PR #226 lane/oc-orch-g12 (6e4dde81 G1/G2 orchestrate role + 275e8717 agy-sonnet model id claude-sonnet-5-5-medium), Hand-Edit, full suite 7559 passed. Needs CI + 2 non-Claude seats (Haiku excluded: Claude hand-edit) e.g. qwen + mimo-pro, then merge + evidence; after merge tell server-L1-main the agy-sonnet fix (routing D-2921, their Sonnet writer was denied claude -p) and workst-L1-autoos. G3 slice + G4 via workst-L1-autoos writers (D-2916/2917); goal: OpenCode L1/L2 path works end to end.
3. Bus lane (D-2911): R2b-2 writer run-31bd47be ENDED 05:2xZ, left UNCOMMITTED edits (wake.py, FLEET-BUS.md, new tests/test_fleet_bus_wake_daemon.py, deploy/systemd/fleet-wake.service.example) - verify vs brief + run its tests before commit (D-2889) (log $J/r2b2.log, wt ~/code/plangraph-worktrees/bus-fix-r2b, tools ~/.local/share/plangraph/build-L1-bus-tools/) -> verify vs brief + tests -> 1 qwen seat on 9287648a..head -> PR to phase/rt-p1-bus -> merge main once -> phase GO-REQ (HIGH). Then D-2913 cross-host bus round trip with workst-L1-autoos.
4. Routing beat 05:2xZ: RT-P1-EXEC-OWNERSHIP in_review 49 h, last before first dogfood: keep SQ12 (workst) first after bus/ENFORCE; when SQ12 merges send routing the close path (evidence + seats).
5. D-2918(B): move ad-hoc scripts (ls9-*, gspawn2.sh, postm.sh, seat/chain/stallwatch) into plangraph tools/ or ops/ with tests + --dry-run, small PRs (workst writer or Hand-Edit).
6. SPAWN-ISOLATION-INPLACE config-guard fix (router reopen / new key needed; done items cannot reopen, D-2921) ; SPAWN-PACKET-SCRUB after ORCH-EFFICIENCY-ENFORCE; SWP fix log round A.

## Traps learned this gen
- Done items cannot be reopened (keeper bad_state): one Plan-Item per PR; split work = split keys before merging part 1.
- Retrailer without force-push: new branch + new PR, close old.
- Host: coding.vm agent-run cap 4 (D-2706); route new writers to workst.
