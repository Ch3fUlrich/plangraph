# build-L1 card · gen-29 · 2026-10-25 05:3xZ · NO cron · hb loop = jobs/975daff2/tmp/hb.sh (3x8min, restart on exit) over ops/hb-keys.txt
Rules: D-2643/2644 batch review at end; D-2534 phase-batch; D-2539 Haiku first w/o Claude writer; D-2540 first review 2 families, re-review 1; D-2511 seats != any writer family.
Writers D-2683/2684: agy gemini > mimo-pro > OR free/opencode free (~01Z) > mimo-flash; never Claude; cx out ~10-30. agy -p never a seat (L24). D-2686: no spawn while MemAvailable < 4000 MB.
Spawn: python3 ~/code/AutoOS/tools/autoos-agent.py run --isolate --client agy --model gemini-3.8-flash | --client opencode --model mimo/mimo-v2.6-pro --card '{"complexity":"standard","privacy":"public","role":"implement"}' --item-key K --title T "$(cat brief)" (cwd = lane worktree). Briefs/seats: ~/code/plangraph-worktrees/briefs/lanes-d2657{,/seats}.
Handoff = status/build-L1.handoff.md. Control repo local only: git push control build-L1.
| Item | State / next |
|---|---|
| #164 RT-OVERSIGHT-KEEPER-SCOPE (HIGH) | r8 Haiku seat fail (5 new). r9 writer agy gemini RUNNING (bg bfunu1nj1, run.txt seats/scope-r9.run.txt, brief scope-r9.md, 3 files) = round 2 of 3 (D-2691). Next: delta re-seat Haiku + non-Claude (deepseek-direct if serves else opencode free glm/deepseek-free); pass -> GO-REQ naming round -> new PR from lane/keeper-scope-r6, close old #164 -> mergepath |
| #183 ROUTER-LIVENESS-WATCHDOG | qwen seat fail; r2 writer mimo-pro RUNNING (bg bhnv9oyxz, seats/liveness-r2.run.txt, brief liveness-r2.md). Next: delta qwen seat -> MERGE-READY -> mergepath 183 |
| #184 HARDENING (3 keys) | head 5a4e8a8b CI red pytest-postgres (pg job runs all affected -> gitleaks test). Gate-Edit D-2692 GO; brief hardening-ci-fix.md; writer agy gemini when mem >= 4000; then 1 seat (standard) -> mergepath -> evidence TEST-SPEED-AFFECTED + RUN-EXIT-NOTIFY, submit 3, prune hb-keys |
| RT-P1-EXEC-OWNERSHIP (HIGH) | D-2691: Haiku tool-less seat as 2nd family; prompt seats/exec-own.haiku.prompt.txt (sha 8142cf0a…, diff #140+#160) ready; run when mem ok, then request close |
| D-2691 closed 6 | KG-LINK, LESSONS-TO-ALGO, SEAT-VERDICT-QODER, WAIT-GUARD, SEAT-FAMILY-WRITER, PG-PUBLIC-READY |
| #166 KEEPER-PG-RECONNECT | after #164 |
| #139 KEEPER-TRAILER-MULTI | after #166 |
| KEEPER-WATCHDOG-ROLE (HIGH) | after #164; spec watchdog-principal.md; schema vs build-L1-bus |
| PG public cut | OS-PG-PUBLIC-PUSH operator; re-cut on "push window now" (pg-public-cut-plan.md) |
| LIVE-SYNC-9 | live 2d61e281; OS-KEEPER-LS9-MIGRATE operator, then z8 drop-in |
| L2s | build-L2-review-method d3c8d22a running (watch MERGE-READY); build-L1-bus peer |
Traps: zsh -> bash -c; mergepath needs claim, no submit; no xdist, affected tests only; never dump environ; Haiku seat = claude -p --model haiku --tools "" --strict-mcp-config --mcp-config jobs/975daff2/tmp/nomcp.json from tmp/empty (stdin).
