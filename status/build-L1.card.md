# build-L1 card · gen-29 · 2026-10-25 05:3xZ · NO cron · hb loop = jobs/975daff2/tmp/hb.sh (3x8min, restart on exit) over ops/hb-keys.txt
Rules: D-2643/2644 batch review at end; D-2534 phase-batch; D-2539 Haiku first w/o Claude writer; D-2540 first review 2 families, re-review 1; D-2511 seats != any writer family.
Writers D-2683/2684: agy gemini > mimo-pro > OR free/opencode free (~01Z) > mimo-flash; never Claude; cx out ~10-30. agy -p never a seat (L24). D-2686: no run here while MemAvailable < 4000 MB (incl. projected RSS; qwen seat ~880 MB) -> workst-L1-autoos.
Spawn: python3 ~/code/AutoOS/tools/autoos-agent.py run --isolate --client agy --model gemini-3.8-flash | --client opencode --model mimo/mimo-v2.6-pro --card '{"complexity":"standard","privacy":"public","role":"implement"}' --item-key K --title T "$(cat brief)" (cwd = lane worktree). Briefs/seats: ~/code/plangraph-worktrees/briefs/lanes-d2657{,/seats}.
Handoff = status/build-L1.handoff.md. Control repo local only: git push control build-L1.
| Item | State / next |
|---|---|
| #164 RT-OVERSIGHT-KEEPER-SCOPE (HIGH) | r9 88bc89d4: Haiku+dsfree delta FAIL. r10 = ROUND 3/3 brief scope-r10.md (group A store_oversight/auth/store, group B db.py) queued on workst-L1-autoos (lane/keeper-scope-r10a, -r10b from 88bc89d4; behind its 3-writer cap). Next: merge both into r6, keeper tests, delta seat (Haiku + non-Claude), pass -> GO-REQ round 3; fail -> replan |
| #183 ROUTER-LIVENESS-WATCHDOG | MERGED 31331e80 (main green, evidence). Item OPEN until install evidence; GO-REQ install + leaf LIVENESS-WATCHDOG-LOWS JSON sent routing 06:0xZ |
| #184 HARDENING (3 keys) | acf975ea Haiku seat fail: HIGH --within vacuous + LOW loadgroup accepted, 2 MED refuted -> r2 writer on workst (L3-REQ #3); triage sent routing. Then mergepath, evidence, submit 3 |
| RT-P1-EXEC-OWNERSHIP (HIGH) | workst Haiku seat FAIL (exec-own.seat-haiku.out 773c8e6f): 2 REAL HIGH (fence cap 16, park skips forward table), reap partly refuted. Proposed leaf RT-P1-EXEC-OWNERSHIP-SQ12 to routing; await ruling |
| PG-L3-MAX-TOKENS (D-2694/2695) | claimed e1; writer queued workst (C:\fleet\wt\pg-l3mt, agy); then 1 seat -> mergepath |
| D-2691 closed 6 | KG-LINK, LESSONS-TO-ALGO, SEAT-VERDICT-QODER, WAIT-GUARD, SEAT-FAMILY-WRITER, PG-PUBLIC-READY |
| #166 KEEPER-PG-RECONNECT | after #164 |
| #139 KEEPER-TRAILER-MULTI | after #166 |
| KEEPER-WATCHDOG-ROLE (HIGH) | after #164; spec watchdog-principal.md; schema vs build-L1-bus |
| PG public cut | OS-PG-PUBLIC-PUSH operator; re-cut on "push window now" (pg-public-cut-plan.md) |
| LIVE-SYNC-9 | live 2d61e281; OS-KEEPER-LS9-MIGRATE operator, then z8 drop-in |
| L2s | build-L2-review-method d3c8d22a running (watch MERGE-READY); build-L1-bus peer |
Traps: zsh -> bash -c; mergepath needs claim, no submit; no xdist, affected tests only; never dump environ; Haiku seat = claude -p --model haiku --tools "" --strict-mcp-config --mcp-config jobs/975daff2/tmp/nomcp.json from tmp/empty (stdin).
