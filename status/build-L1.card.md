# build-L1 card · gen-30 · 2026-10-25 08:2xZ (D-2717) · NO cron · hb loop = jobs/2f3882a9/tmp/hb.sh (3x8min, restart on exit) over ops/hb-keys.txt
Rules: D-2643/2644 batch review at end; D-2534 phase-batch; D-2539 Haiku first w/o Claude writer; D-2540 first review 2 families, re-review 1; D-2511 seats != any writer family.
Writers D-2683/2684: agy gemini > mimo-pro > OR free/opencode free (~01Z) > mimo-flash; never Claude; cx out ~10-30. agy -p never a seat (L24). D-2706: floor MemAvailable 2000 MB (incl. projected RSS), max 4 writers here, rest -> workst-L1-autoos. One compaction = recycle (new session from handoff).
D-2716: no new writers on coding.vm until <=4 agent-run procs; route to workst.
Spawn: python3 ~/code/AutoOS/tools/autoos-agent.py run --isolate --client agy --model gemini-3.8-flash | --client opencode --model mimo/mimo-v2.6-pro --card '{"complexity":"standard","privacy":"public","role":"implement"}' --item-key K --title T "$(cat brief)" (cwd = lane worktree). Briefs/seats: ~/code/plangraph-worktrees/briefs/lanes-d2657{,/seats}.
Handoff = status/build-L1.handoff.md. Control repo local only: git push control build-L1.
D-2719 done: AutoOS ff d386df2e->282abaab.
| Item | State / next |
|---|---|
| DONE gen-30 | #188 0c4e8913, #187 532c95e3, #184 0de64eb5 (3 keys), #190 318f95ce; D-2721 closed 5; #183 DONE |
| STALL-WATCHDOG | SUBMITTED in_review: #191 f0e8a4a2 + #192 9b5ba588; timer code pinned live-stall 9b5ba588; workst task f0e8a4a2 enabled. Asked routing: leaf STALL-WD-WIN-WORKDIR, workst bridge target. Rollback: systemctl --user disable --now plangraph-stall-watchdog.timer; schtasks /DISABLE |
| MODEL-ROUTE-SPAWN (claimed e1) | prod run: code host 3 real spawns (run-667e9a9e, run-b8917744, run-80542f93, qwen leg); workst spawns asked; then 2 non-Claude seats != writer family |
| #164 RT-OVERSIGHT-KEEPER-SCOPE (HIGH) | r9 88bc89d4: Haiku+dsfree delta FAIL. r10 = ROUND 3/3 brief scope-r10.md (group A store_oversight/auth/store, group B db.py) queued on workst (order sq12, r10a, r10b; cap full 08:3xZ). Next: merge both into r6, keeper tests, delta seat (Haiku + non-Claude), pass -> GO-REQ round 3; fail -> replan |
| RT-P1-EXEC-OWNERSHIP-SQ12 (HIGH, D-2703) | writer workst lane/exec-own-sq12; then 2 seats, GO-REQ; parent stays in_review |
| #166 KEEPER-PG-RECONNECT | after #164 |
| #139 KEEPER-TRAILER-MULTI | after #166 |
| KEEPER-WATCHDOG-ROLE (HIGH) | after #164; spec watchdog-principal.md; schema vs build-L1-bus |
| PG public cut | OS-PG-PUBLIC-PUSH operator; re-cut on "push window now" (pg-public-cut-plan.md) |
| LIVE-SYNC-9 | live 2d61e281; OS-KEEPER-LS9-MIGRATE operator, then z8 drop-in |
Traps: zsh -> bash -c; mergepath needs claim, no submit; no xdist, affected tests only; never dump environ; Haiku seat = claude -p --model haiku --tools "" --strict-mcp-config --mcp-config jobs/975daff2/tmp/nomcp.json from tmp/empty (stdin).
