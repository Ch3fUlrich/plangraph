# build-L1 handoff · gen-39 -> gen-40 · 2026-10-26 ~10:00Z
Read the card next to this file. Rules: launch brief + memory + D-2983 (until Thu): NO Claude Sonnet/Opus writers; Claude only as Haiku review seat when no non-Claude family fits; writers cx/gpt-6.1-sol then mimo-v2.6-pro; reviews luna > qwen > glm/nemotron > Haiku; minimal Claude turns, no Agent subagents, wait via one bg command.
Dirs: J0=/home/s/.claude/jobs/0bbdff0b/tmp (gspawn2.sh, glmseat.sh, haikuseat.sh, seatpost2.py, hb.sh), J=/home/s/.claude/jobs/038a08a5/tmp (qseat.sh, archive-to.sh, seats). Copies: ~/.local/share/plangraph/build-L1-seats/gen39. Server seats: ~/.local/share/plangraph/server-seats/.
hb loop J0/hb.sh keeps claims in ops/hb-keys.txt.

## Done gen-39
#229 bus phase merged 643a29ef (judge D-2982), main CI green. #238 D-2966 implement header merged 34d56f0f. #234 host-unredacted a21b2bdd. Archive D-2984/7/8: fleet-archive (PRIVATE) plangraph 44 + autoos 57, Server 10 + Invest 6 on origin.
3 bus items RT-P1-BUS-KEEPER / -DISCOVERY / EXEC-OBSERVED-STATE: released, router set in_review + commit evidence; NO close path (keeper floor) -> D-2994 leave them. Fix item filed: plangraph:KEEPER-FLOOR-LEGACY-CLAIM (p3, AUTH_DOOR, writer sol/mimo + Gate-Edit D-id on request).

## Open, priority order
1. LUNA review leg (critical path: gen phase seat B, EXEC-OBSERVED-STATE seat 2, fleet seats while glm free is exhausted). Lane lane/luna-leg, worktree ~/code/plangraph-worktrees/luna-leg, brief J/brief-luna.md, writer mimo-v2.6-pro via J0/gspawn2.sh (bg; output J/w-luna.json/.wait). Leg added: Leg("cx-gpt-6-luna","subscription","opencode","omniroute/cx-gpt-6-luna","gpt",...,roles=("review",)).
   OPEN QUESTION: served path. Catalog (routing_support/catalog_seed.py + catalog_data.py:474) has cxa/gpt-6-luna-high (Codex ACP provider cxa); workst row harness=None. Router wants catalog seeded with luna + harness on both hosts in this slice. Confirm with routing which path (omniroute/cx-gpt-6-luna via opencode like sol, or cxa ACP) BEFORE the PR; fix the leg model id + seed harness accordingly.
   Then: L1 adds Gate-Edit: D-2984 trailer (writers never write it), CI, qwen seat, merge (standard), re-seed catalog on coding.vm + ask workst to seed.
   After merge: gen phase seat B on genw4/server-phase (bb85c51+) when workst-L1-gen asks (ONLY design files, no data/); EXEC-OBSERVED-STATE luna seat.
2. Server SEAT-REQ semsync lane/semsync-only 50e3ac62..092b4c39 (workst-L1-autoos, owner server-L1-main): seat dir ~/.local/share/plangraph/server-seats/semsync-092b4c39. qwen r1 = bare "VERDICT: PASS" (no evidence; thin), r2 with per-item format running (qwen2/). Send result to server-L1-main [6d0937] + cc workst-L1-autoos. Head tests 16 passed locally.
3. MODEL-ROUTE-SPAWN close: evidence file gen39/mrs-evidence.md. Have: workst run-31a667ae host=bz-wg-pdw028, coding.vm run-4f182aad host=code, luna writer spawn (3rd). Need: 1 witnessed 2-family review (#225 merged) -> then keeper evidence/submit via routing.
4. SQ12: wait workst lane/exec-own-sq12b PR + sha -> qwen seat (vectors: Test-Removal strictness, live-owner rule b) -> close path to routing.
5. D-2918(B) script move into plangraph (gspawn2/qseat/glmseat/haikuseat/seatpost2/archive-to); seatpost2 must write verdict+test_gaming (review.verdict.judge) into evidence.json -- covered by KEEPER-FLOOR-LEGACY-CLAIM part 2.
6. LS10 live keeper -> bus schema: own live-sync step (dump, dry run on copy, GO-REQ with tested commands). G3/G4 later; D-2913 cross-host round trip with workst = next bus evidence (BUS-ANY-AGENT-CLI), RT-P1-BUS-WAKE needs live timer removal.
Lessons: seats on a commit series see stale intermediate states -> seat the net final diff. glm free quota out on both hosts (D-2989): probe before burning runs.
