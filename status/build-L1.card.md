# build-L1 card · gen-29 · 2026-10-25 07:0xZ (recycle D-2706) · NO cron · hb loop = jobs/975daff2/tmp/hb.sh (3x8min, restart on exit) over ops/hb-keys.txt
Rules: D-2643/2644 batch review at end; D-2534 phase-batch; D-2539 Haiku first w/o Claude writer; D-2540 first review 2 families, re-review 1; D-2511 seats != any writer family.
Writers D-2683/2684: agy gemini > mimo-pro > OR free/opencode free (~01Z) > mimo-flash; never Claude; cx out ~10-30. agy -p never a seat (L24). D-2706: floor MemAvailable 2000 MB (incl. projected RSS), max 4 writers here, rest -> workst-L1-autoos. One compaction = recycle (new session from handoff).
Spawn: python3 ~/code/AutoOS/tools/autoos-agent.py run --isolate --client agy --model gemini-3.8-flash | --client opencode --model mimo/mimo-v2.6-pro --card '{"complexity":"standard","privacy":"public","role":"implement"}' --item-key K --title T "$(cat brief)" (cwd = lane worktree). Briefs/seats: ~/code/plangraph-worktrees/briefs/lanes-d2657{,/seats}.
Handoff = status/build-L1.handoff.md. Control repo local only: git push control build-L1.
| Item | State / next |
|---|---|
| #164 RT-OVERSIGHT-KEEPER-SCOPE (HIGH) | r9 88bc89d4: Haiku+dsfree delta FAIL. r10 = ROUND 3/3 brief scope-r10.md (group A store_oversight/auth/store, group B db.py) queued on workst-L1-autoos (lane/keeper-scope-r10a, -r10b from 88bc89d4; behind its 3-writer cap). Next: merge both into r6, keeper tests, delta seat (Haiku + non-Claude), pass -> GO-REQ round 3; fail -> replan |
| #183 ROUTER-LIVENESS-WATCHDOG | MERGED 31331e80; routing 0e3f3045; liveness-watchdog@router.timer ENABLED 06:08Z (drop-in -> pinned live-liveness 31331e80, keep). Close requested from routing. Rollback: systemctl --user disable --now liveness-watchdog@router.timer |
| LIVENESS-WATCHDOG-LOWS (D-2705) | writer mimo-pro local (seats/liveness-lows.run.txt); no re-seat -> CI -> mergepath |
| #184 HARDENING (3 keys) | mergepath at a8efa6d5 (tmp/mp184.log); then evidence merge sha to TEST-SPEED-AFFECTED + RUN-EXIT-NOTIFY, submit 3 |
| RT-P1-EXEC-OWNERSHIP-SQ12 (HIGH, D-2703) | writer workst lane/exec-own-sq12; then 2 seats, GO-REQ; parent stays in_review |
| PG-L3-MAX-TOKENS #187 | r2 writer mimo-pro local; CI red import guard -> r3 move SEED_MODELS to plangraph/catalog_data.py (decision a); Haiku delta seat; mergepath |
| #166 KEEPER-PG-RECONNECT | after #164 |
| #139 KEEPER-TRAILER-MULTI | after #166 |
| KEEPER-WATCHDOG-ROLE (HIGH) | after #164; spec watchdog-principal.md; schema vs build-L1-bus |
| PG public cut | OS-PG-PUBLIC-PUSH operator; re-cut on "push window now" (pg-public-cut-plan.md) |
| LIVE-SYNC-9 | live 2d61e281; OS-KEEPER-LS9-MIGRATE operator, then z8 drop-in |
| #185 REVIEW-METHOD-SETTING | MERGED d85d38ff, submitted; next leaf REVIEW-METHOD-SETTING-LOWS |
Traps: zsh -> bash -c; mergepath needs claim, no submit; no xdist, affected tests only; never dump environ; Haiku seat = claude -p --model haiku --tools "" --strict-mcp-config --mcp-config jobs/975daff2/tmp/nomcp.json from tmp/empty (stdin).
