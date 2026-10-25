# build-L1 card · gen-31 · 2026-10-25 11:58Z (D-2732) · NO cron · hb loop = jobs/b4d163a0/tmp/hb.sh (restart on exit; session OOM-restarted 3x ~11:50Z)
D-2739 (operator): one task loops (no follow-up items), design first for HIGH/>400 lines, slices <=400, 2 text seats, MED in body=changes, fixer>=writer never flash, round>3 -> Opus replan, merge main once before final review.
D-2749: AutoOS spawner retired; all runs via plangraph spawner; missing feature -> BLOCKED to routing, no fallback.
D-2744: no deepseek leg (writer or seat, incl opencode free deepseek); mimo-pro, mimo-flash seats only.
D-2754: design/replan by me or in-process Opus Agent; code writers+seats via plangraph spawner, pin --leg mimo-v2.6-pro (never deepseek); in place on coding.vm OK for opencode/mimo/qodercli w/ .git/config diff check, no agy in place.
Rules: D-2643/2644 batch review at end; D-2534 phase-batch; D-2539 Haiku first w/o Claude writer; D-2540 first review 2 families, re-review 1; D-2511 seats != any writer family.
Writers D-2683/2684: agy gemini > mimo-pro > OR free/opencode free (~01Z) > mimo-flash; never Claude; cx out ~10-30. agy -p never a seat (L24). D-2706: floor MemAvailable 2000 MB (incl. projected RSS), max 4 writers here, rest -> workst-L1-autoos. One compaction = recycle (new session from handoff).
Router 10:3xZ: D-2706 floor 2000 MB incl projected RSS REPLACES D-2686 4000.
D-2716: no new writers on coding.vm until <=4 agent-run procs; route to workst.
Spawn: python3 ~/code/AutoOS/tools/autoos-agent.py run --isolate --client agy --model gemini-3.8-flash | --client opencode --model mimo/mimo-v2.6-pro --card '{"complexity":"standard","privacy":"public","role":"implement"}' --item-key K --title T "$(cat brief)" (cwd = lane worktree). Briefs/seats: ~/code/plangraph-worktrees/briefs/lanes-d2657{,/seats}.
Handoff = status/build-L1.handoff.md. Control repo local only: git push control build-L1.
D-2719 done: AutoOS ff d386df2e->282abaab.
| Item | State / next |
|---|---|
| DONE gen-30 | #188 0c4e8913, #187 532c95e3, #184 0de64eb5, #190 318f95ce, #195 ab44789b (main-red fix of #194); D-2721 closed 5 |
| STALL-WATCHDOG + STALL-WD-WIN-WORKDIR | both SUBMITTED in_review (9b5ba588; #194 eec0eac1 + #195). workst task re-rendered ab44789b no hand patch. Rollback: systemctl --user disable --now plangraph-stall-watchdog.timer; schtasks /DISABLE |
| STALL-WD-DELIVERY-QUIET | DONE #197 5d7873c6 submitted; code repin 5d7873c6 done (D-2731), workst repin asked |
| SPAWN-ISOLATION-INPLACE / SPAWN-ORPHAN-REAP (D-2731) | build-L2-spawn-hardening 249a3221 launched D-2734, reports MERGE-READY to me (brief briefs/lanes-d2657/l2-spawn-hardening/brief.md); lanes pushed at 5d7873c6 |
| CI-SELECT-DEPLOY-REFS | DONE #196 11421fc4 main CI 40183692416 green, evidence + submitted |
| MODEL-ROUTE-SPAWN | D-2732 GO: #198 3c982c4d full suite 7166 pass + CI green; MERGED c08badc2 main CI 40182859958 green; evidence after ISOLATION (D-2732 opt b); also #204 defc1c89 |
| #164 RT-OVERSIGHT-KEEPER-SCOPE (HIGH) | DONE merged 7be789f3 main CI 40189649014 green, evidence commit+ci + D-2747, submitted |
| RT-P1-EXEC-OWNERSHIP-SQ12 (HIGH, D-2703) | writer workst run 20261025-070241-exec-sq12 (mimo-pro) running; then 2 seats, GO-REQ; parent stays in_review |
| #166 KEEPER-PG-RECONNECT (HIGH) | claimed ep2. Opus design DONE briefs/lanes-d2657/pg-reconnect-design.md: DROP draft PR #166, 3 slices S1 503-no-half-write ~300, S2 exit 75 + lock-conn watchdog ~280, S3 reconnect between requests ~330; DC fact: incident socket = lock conn. Next: S1 mimo-pro writer (after #204) |
| #139 KEEPER-TRAILER-MULTI | after #166 |
| KEEPER-WATCHDOG-ROLE (HIGH) | dep #164 now in_review -> after #166; spec watchdog-principal.md |
| PG public cut | OS-PG-PUBLIC-PUSH operator; re-cut on "push window now" (pg-public-cut-plan.md) |
| LIVE-SYNC-9 | live 2d61e281; OS-KEEPER-LS9-MIGRATE operator, then z8 drop-in |
D-2735: no in-place plangraph spawns on workst until ISOLATION merges; before merging r10b commits check EOL churn (git diff --stat -w vs plain).
| D-2756 mimo baseURL | DONE #204 merged defc1c89 main CI 40192268968 green (evidence on MODEL-ROUTE-SPAWN pending claim, like #198); probe on main run-c115b3c5: 'Authentication required' -> key wiring + watchdog token (D-2758 1,3) HELD for operator line in this session (D-2759) |
| D-2759 keys | op OK; code watchdog.env PG_TOKEN_FILE set; workst BLOCKED (no spawner.token, Windows task ignores env file; op there). Key: ref lane/host-keys-ref 791a5a1c (gate-refused Claude), mimo-pro writer bg on lane/spawner-host-keys (wt spawner-host-keys, spawner from wt spawner-omniroute-key) -> 2 seats -> GO-REQ -> merge -> tell prox-L1 mimo up |
| D-2761/2762 | disk 0->13G (sandbox skips: job tmp sb-skipped-d2761.txt); repos.json both hosts |
| mimo-up notify | send prox-L1-main one line when mimo legs work (D-2750: mimo-pro its only reviewer) |
Traps: zsh -> bash -c; mergepath needs claim, no submit; no xdist, affected tests only; never dump environ; Haiku seat = claude -p --model haiku --tools "" --strict-mcp-config --mcp-config jobs/975daff2/tmp/nomcp.json from tmp/empty (stdin).
Opencode free seats work again (orphan serve pid 852130 stopped, D-2731). Host lacks `python` binary: test_smoke_checks_default_dry_run fails locally.
