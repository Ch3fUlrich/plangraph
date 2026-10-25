# build-L1 card · gen-30 · 2026-10-25 10:0xZ RECYCLE (handoff rows supersede) (D-2717) · NO cron · hb loop = jobs/2f3882a9/tmp/hb.sh (3x8min, restart on exit) over ops/hb-keys.txt
Rules: D-2643/2644 batch review at end; D-2534 phase-batch; D-2539 Haiku first w/o Claude writer; D-2540 first review 2 families, re-review 1; D-2511 seats != any writer family.
Writers D-2683/2684: agy gemini > mimo-pro > OR free/opencode free (~01Z) > mimo-flash; never Claude; cx out ~10-30. agy -p never a seat (L24). D-2706: floor MemAvailable 2000 MB (incl. projected RSS), max 4 writers here, rest -> workst-L1-autoos. One compaction = recycle (new session from handoff).
D-2716: no new writers on coding.vm until <=4 agent-run procs; route to workst.
Spawn: python3 ~/code/AutoOS/tools/autoos-agent.py run --isolate --client agy --model gemini-3.8-flash | --client opencode --model mimo/mimo-v2.6-pro --card '{"complexity":"standard","privacy":"public","role":"implement"}' --item-key K --title T "$(cat brief)" (cwd = lane worktree). Briefs/seats: ~/code/plangraph-worktrees/briefs/lanes-d2657{,/seats}.
Handoff = status/build-L1.handoff.md. Control repo local only: git push control build-L1.
D-2719 done: AutoOS ff d386df2e->282abaab.
| Item | State / next |
|---|---|
| DONE gen-30 | #188 0c4e8913, #187 532c95e3, #184 0de64eb5, #190 318f95ce, #195 ab44789b (main-red fix of #194); D-2721 closed 5 |
| STALL-WATCHDOG + STALL-WD-WIN-WORKDIR | both SUBMITTED in_review (9b5ba588; #194 eec0eac1 + #195). workst task re-rendered ab44789b no hand patch. Rollback: systemctl --user disable --now plangraph-stall-watchdog.timer; schtasks /DISABLE |
| STALL-WD-DELIVERY-QUIET | DONE #197 5d7873c6 submitted; code repin 5d7873c6 done (D-2731), workst repin asked |
| SPAWN-ISOLATION-INPLACE / SPAWN-ORPHAN-REAP (D-2731) | created open, owner build-L1; need briefs + writers; ISOLATION gates MODEL-ROUTE-SPAWN |
| CI-SELECT-DEPLOY-REFS | PR #196 head 97befbe4, re-review PASS; full suite -> mergepath |
| MODEL-ROUTE-SPAWN | PR #198 head 3c982c4d re-review PASS; claim refused (gated on SPAWN-ISOLATION-INPLACE) -> routing decides merge path |
| #164 RT-OVERSIGHT-KEEPER-SCOPE (HIGH) | r10a DONE workst lane/keeper-scope-r10a 388bb1c4 (A1-A8 fixed, CI 40180813017); 4 PG oversight fails pre-existing on r9 base 88bc89d4 (check before GO). r10b pending workst. Next: merge both into r6, keeper tests, delta seats, GO-REQ |
| RT-P1-EXEC-OWNERSHIP-SQ12 (HIGH, D-2703) | writer workst lane/exec-own-sq12; then 2 seats, GO-REQ; parent stays in_review |
| #166 KEEPER-PG-RECONNECT | after #164 |
| #139 KEEPER-TRAILER-MULTI | after #166 |
| KEEPER-WATCHDOG-ROLE (HIGH) | after #164; spec watchdog-principal.md; schema vs build-L1-bus |
| PG public cut | OS-PG-PUBLIC-PUSH operator; re-cut on "push window now" (pg-public-cut-plan.md) |
| LIVE-SYNC-9 | live 2d61e281; OS-KEEPER-LS9-MIGRATE operator, then z8 drop-in |
Traps: zsh -> bash -c; mergepath needs claim, no submit; no xdist, affected tests only; never dump environ; Haiku seat = claude -p --model haiku --tools "" --strict-mcp-config --mcp-config jobs/975daff2/tmp/nomcp.json from tmp/empty (stdin).
Opencode free seats work again (orphan serve pid 852130 stopped, D-2731). Host lacks `python` binary: test_smoke_checks_default_dry_run fails locally.
