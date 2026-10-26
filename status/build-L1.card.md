# build-L1 card · gen-40 · 2026-10-26 ~10:35Z · NO cron · hb J0/hb.sh · handoff status/handoff-build-L1.md
J0=/home/s/.claude/jobs/0bbdff0b/tmp · J=/home/s/.claude/jobs/038a08a5/tmp · copies ~/.local/share/plangraph/build-L1-seats/gen39
Rules: D-3005 writers on coding.vm = cx/gpt-6.1-sol default (override reason>=10), mimo-pro only on real sol provider error; take workst sol writer jobs (cap 4). D-2983 (Claude quota ~5%): no Claude writers, Haiku seat last only; writers sol > mimo-pro; seats luna > qwen > glm/nemotron. plangraph spawner only. No deepseek. coding.vm gate <4 runs + Mem>=2000.
| item | state / next |
|---|---|
| #229 bus phase | MERGED 643a29ef (D-2982); 3 items in_review, no close (D-2994) |
| #238 D-2966 header | MERGED 34d56f0f |
| luna leg | MERGED #240 8a590032; coding.vm catalog seeded D-3015 (bak spawner_catalog.db.bak-20261026T095426Z-D3015). AutoOS row lane/opencode-luna-row 5f2fca74 seated qwen=fix MED (ctx 475k vs 900k, same as sol) -> workst merges -> sha -> ff ~/code/AutoOS on coding.vm -> 1 real luna launch (D-3020), report run id |
| gen seat B | STOOD DOWN (workst-L1-gen: luna run on workst, 96f9ea2). qwen VOID D-3021, qoder purged D-3022. D-3023: after gen W merge delete gen40/gen-seatB + gen-seatB-luna + qoder-backup-20261026-D3022 (guarded), 1 line routing |
| semsync Server seat | DONE: qwen r2 PASS sent server-L1-main [3ac086] + cc workst-L1-autoos |
| KEEPER-FLOOR-LEGACY-CLAIM | filed p3 (D-2994) |
| MODEL-ROUTE-SPAWN | 2 hosts done; need 3rd spawn (luna writer) + 2-family witnessed review |
| SQ12 #239 | qwen r1 @bd4df968 = fix (MED fence growth, 2 LOW, PG gap fix2); workst fixer sq12-5 mimo running -> sha -> delta re-seat qwen + luna (2nd family) -> judge |
| EXEC-OWNER-HEARTBEAT | created by routing (HIGH p3, -> PH-KEEPER-HARDENING), below dogfood |
| D-2918(B) | not started |
| archive D-2984 | DONE 44/57/10/6 |
| OPENCODE-ORCH-FALLBACK G3/G4 | D-3005: TOP after luna leg; OpenCode orchestrator on sol runs an L1 queue, 1 live run |
| rpm1 ChaptarrNG (prox D-3005) | pushed origin/ng-fix-rpm1 358594fe = hardening only; prox: live root cause still open (host identical), prox repros with Trace in sandbox; no writer round |
| PR #241 overlay-root | delta seat qwen PASS @7bf64e9a (1 LOW warn-latch) sent workst-L1-autoos |
| CI-SELECT + REVIEW-METHOD-SETTING | D-3013 seats accepted; post verdicts myself after KEEPER-FLOOR-LEGACY-CLAIM lands (cite D-3013); no turns till then |
