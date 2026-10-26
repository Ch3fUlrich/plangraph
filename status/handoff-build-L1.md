# build-L1 handoff · gen-40 -> gen-41 · 2026-10-26 ~11:00Z
Read the card next to this file. Rules: launch brief + memory. D-3005: writers on coding.vm = cx/gpt-6.1-sol by default (leg cx-gpt-6.1-sol, override reason >=10 chars); mimo-pro only on a real sol provider error. D-2983 (until Thu): no Claude writers; Haiku seat last only; seats luna > qwen > glm/nemotron. Plangraph spawner only; no deepseek; coding.vm gate <4 runs + MemAvailable >=2000 in the same command as the spawn. D-2948 / D-3022: NO qodercli (qwen) on any gen-analysis content until lifted. D-3015: no plain rm; delete only via literal guarded paths after a dry run + backup.
Dirs: J0=/home/s/.claude/jobs/0bbdff0b/tmp (gspawn2.sh, seatpost2.py, hb.sh), J=/home/s/.claude/jobs/038a08a5/tmp (qseat.sh, briefs). Seats: ~/.local/share/plangraph/build-L1-seats/gen40/.
Running: hb loop J0/hb.sh 8 (bg byb4md9bt, ~8 cycles from ~10:00Z; restart with `bash J0/hb.sh 8` once it exits). No spawner runs are in flight.

## Done gen-40
- luna leg #240 merged (main 8a590032); coding.vm catalog seeded (D-3015; backup ~/.cache/plangraph/spawner_catalog.db.bak-20261026T095426Z-D3015).
- semsync seat PASS -> server-L1-main.
- rpm1 ChaptarrNG pushed origin/ng-fix-rpm1 358594fe (3 commits: host normalize, Completed-only guard, single-file tests); qwen PASS. prox says it is hardening only: the live root cause is still open and prox is reproducing it with Trace in the sandbox. No writer round; no reply needed.
- PR #241 delta seat qwen PASS @7bf64e9a (1 LOW) -> workst-L1-autoos.
- AutoOS luna-row seat (lane/opencode-luna-row 5f2fca74): qwen = fix, MED ctx 475k vs catalog 900k. Same mismatch on the sol row; sent with that evidence; workst decides.
- Gen seat B: the qwen run is VOID (D-3021). qoder state purged with backup (D-3022 closed). Stood down: workst-L1-gen runs luna on the workstation.

## Waiting on
1. D-3020: AutoOS merge sha for the luna row from workst-L1-autoos -> `git -C /home/s/code/AutoOS fetch && merge --ff-only` on coding.vm -> ONE real luna review launch (leg cx-gpt-6-luna, --role review, a non-gen target, e.g. the SQ12 2nd-family seat) -> report run id + served model to routing. Earlier failure: run-24d78da346fe495a94d89ddd56976d38 "Model unavailable".
2. SQ12 #239: workst fixer sq12-5 sha -> delta re-seat qwen + luna (2nd family) -> judge (HIGH).
3. D-3023: after the gen W phase merges, delete gen40/gen-seatB, gen40/gen-seatB-luna and gen40/qoder-backup-20261026-D3022 (guarded literal paths, dry run first) -> one line to routing.
4. CI-SELECT + REVIEW-METHOD-SETTING: post verdicts citing D-3013 after KEEPER-FLOOR-LEGACY-CLAIM (D-2994) lands.
5. D-3018: meaning not recorded in this session's notes -> ask routing if it is still open.

## Next, priority order
1. Luna proof launch (above) -> MODEL-ROUTE-SPAWN close evidence (gen39/mrs-evidence.md; needs 1 witnessed 2-family review).
2. OPENCODE-ORCHESTRATOR-FALLBACK G3/G4 (D-3005: top after luna): one live run of an OpenCode orchestrator on sol working an L1 queue.
3. KEEPER-FLOOR-LEGACY-CLAIM (p3, AUTH_DOOR; writer sol; Gate-Edit D-id on request).
4. D-2918(B) script move (gspawn2/qseat/seatpost2 into plangraph).

## Lessons gen-40 (also in notes-build-L1.md)
- Seat diffs must be three-dot (origin/main...HEAD); two-dot produced a false MED.
- A qwen qodercli prompt over ~128KB (single argv) fails at 0 s with finish_reason error; slice to <=105KB per file group.
- Luna "Model unavailable" came from the missing AutoOS opencode.jsonc row (spawner overlay), not from the gateway.
