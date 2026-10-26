# build-L1 card · gen-38 -> 39 handoff · 2026-10-26 07:05Z · NO cron · hb J0/hb.sh · handoff status/handoff-build-L1.md
J0=/home/s/.claude/jobs/0bbdff0b/tmp (scripts) · J=/home/s/.claude/jobs/624c6980/tmp (gen-38 seats/logs)
Rules: D-2928 brief + memory. plangraph spawner only (D-2918). No deepseek (D-2744). glm LAST (D-2913). Seat family != every writer family (D-2511, D-2926).
Host gate: <4 opencode/qodercli/agy runs + MemAvailable>=2000 MB, checked in the spawn cmd. Hand-edit <=50 lines with a Hand-Edit trailer, never AUTH_DOOR.
agy gemini quota out ~111h (from 06:5xZ 10-26). Seats available: qwen qodercli, Haiku (only without a Claude writer), glm free (last), OR free.
| item | state / next |
|---|---|
| #229 RT-P1-BUS phase | R2c lane/phase-fix-r2c 747ff866 (3 commits, run-4b8d34c9) -> full suite -> PR into phase -> CI -> qwen seat (api.py hunk named) -> phase GO-REQ (judge) |
| #225 REVIEW-WITNESS-OPENCODE | head c43a40dc (r6 Hand-Edit). Writers mimo+anthropic -> no Haiku. Delta seat qwen + glm/OR free -> CI -> GO-REQ -> merge -> evidence -> post |
| MODEL-ROUTE-SPAWN | PR #231 slice c+d (run-f53b0d6f). CI + 2 seats -> GO -> merge -> provision prod catalog DB -> evidence: 3 spawns/2 hosts + 1 witnessed 2-family review (after #225); (b) skill = workst 3a1a110b not merged |
| SQ12 (workst) | SQ12.1 running, SQ12.2 next -> lane/exec-own-sq12b PR -> my qwen seats -> close path. LAST gate before dogfood |
| Server seats | DONE: tdarr + booklore APPROVE, sent |
| #230 exempt | MERGED 48a2560f |
| #226 | MERGED 7fb8c2d9 |
| D-2918(B) script move | not started |
| G3 / G4 | workst G3 (--standalone in review/opencode.py) / bus round trip later |
| main-ro | refreshed 48a2560f |
