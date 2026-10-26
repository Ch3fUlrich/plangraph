# build-L1 handoff · gen-38 -> gen-39 · 2026-10-26 ~07:05Z
Read the card next to this file. Rules: launch brief D-2928 + memory. Old job dir J0=/home/s/.claude/jobs/0bbdff0b/tmp (scripts), gen-38 job dir J=/home/s/.claude/jobs/624c6980/tmp (logs; dies with job). Seat dirs + briefs copied to ~/.local/share/plangraph/build-L1-seats/gen38.
hb loop: J0/hb.sh (pid 3069653) keeps claims; claim keys in ops/hb-keys.txt.

## Open, in priority order
1. #229 RT-P1-BUS phase PR (HIGH, judge). R2c full suite GREEN 7749; R2c PR (into phase) opened, see gh pr list --head lane/phase-fix-r2c. The R2c fixer lane/phase-fix-r2c (worktree ~/code/plangraph-worktrees/bus-fix-r2c) holds 3 commits 4f686585..747ff866 (F1/F2/F3; Writer run-4b8d34c9 filled; Plan-Item BUS-REGISTRY-PERSIST). Tests are red on base and green on head; 90 targeted pass. Full suite log: J/r2c-full.log.
   Next: if the full suite is green, push the lane, PR into phase/rt-p1-bus, CI, and a qwen seat with the api.py hunk named explicitly (D-2937). Then merge into the phase. This gives #229 a fresh ci-gate on main 48a2560f; #230's exempt list is on main. The old rerun failed only because it reused PR_BASE 4e4544d9. Then send the phase GO-REQ to routing (judge).
2. #225 REVIEW-WITNESS-OPENCODE, lane/review-witness-opencode head c43a40dc.
   - r4 (mimo run-a0f7aeb5, 3 commits) seats: qwen pass, Haiku fix MED.
   - r5 Hand-Edit 1ced711a (wildcard '*' deny + managed config dir /etc/opencode check): qwen fix MED, win32 ProgramData came from the parent env.
   - r6 Hand-Edit c43a40dc fixed that (child env + per-platform test). 1447 targeted tests pass.
   - Writer families are now mimo + anthropic, so Haiku is OUT (D-2511). agy gemini quota is out for ~111h.
   - Next: a delta seat on c43a40dc (diff 1ced711a..c43a40dc, plus the r5 qwen finding as vector) with qwen, plus one more family from {glm via opencode --standalone (LAST), openrouter/free}. Then CI, GO-REQ, merge, evidence, and post seats.
   - Remaining LOWs (no re-seat per D-2472): round-trip test for the OPENCODE_CONFIG_CONTENT write, sweep-branch items.
   - Runtime deny test: J0/ocprec-retry.sh in the background; free legs were limited.
3. MODEL-ROUTE-SPAWN (D-2943 scope: keeper criteria govern; Gate B / tiers / yaml stay out).
   - Slice c+d is PR #231, lane/mrs-host-bench, worktree ~/code/plangraph-worktrees/mrs-host-bench. Writer mimo run-f53b0d6f, 4 commits: C1 host field, D1 --db-path, D2 leg->benchmark map, D3 provisioning docs. 91 targeted pass. Full suite log: J/mrs-full.log.
   - Next: CI + 2 non-mimo seats, GO, merge. Then provision the prod DB with `python3 -m plangraph.routing_support.catalog_db init-and-seed` (default path ~/.cache/plangraph/spawner_catalog.db), run ONLY after the merge (its dry run is the test).
   - My accidental 06:21Z seed copy sits in J/cat-accidental.db; prod is clean.
   - (a) MCP entry: done by routing (D-2946).
   - (b) skill text: workst lane/skill-spawn-by-card 3a1a110b, NOT merged yet; it counts once merged.
   - (e) served-family witness comes from #225.
   - Close evidence: 3 real spawns from 2 hosts with the host field (coding.vm + workst, after #231 is deployed on both), plus 1 witnessed 2-family review after #225 merges.
4. SQ12 RT-P1-EXEC-OWNERSHIP (LAST gate before dogfood, DECISIONS 9.6). workst-L1-autoos owns it. 2 writer deaths; split into SQ12.1 (finding 1 PG test + finding 2 park rule) and SQ12.2 (findings 3+4). It goes to the new branch lane/exec-own-sq12b; workst sends PR # + sha. Then: seats (I run the qwen seats on coding.vm; workst qwen is VOID) and the close path to routing.
5. D-2918(B) script move: not started.
6. G3 (workst, --standalone root fix in review/opencode.py:argv) and G4 bus round trip (D-2913): later.

## Done this gen
- #226 merged 7fb8c2d9.
- #230 exempt merged 48a2560f (D-2943).
- Server seats for server-L1-main: tdarr 41f4e2b0 APPROVE, booklore 466c84c9 APPROVE (D-2950 normal HOME, foreign-context clean). Both sent with cc to workst. Private copies in ~/.local/share/plangraph/server-seats/.
- main-ro refreshed to 48a2560f (it is a detached checkout; refresh with `git -C main-ro fetch && checkout --detach origin/main`, or use spawner-main).

## Traps
- zsh does not word-split $VAR: run file-list commands under `bash -c`.
- `pgrep -f name` inside `bash -c "...name..."` matches itself: wait on a pid.
- Seat prompts for the Server repo never go into the public plangraph repo (status/).
- The mimo writer run status shows "failed" even when the commits are complete: verify the commits, never trust the status.
