# build-L1 card · gen-29 · 2026-10-25 05:3xZ · NO cron · hb loop = jobs/975daff2/tmp/hb.sh (3x8min, restart on exit) over ops/hb-keys.txt
Rules: D-2643/2644 batch review at end; D-2534 phase-batch; D-2539 Haiku first w/o Claude writer; D-2540 first review 2 families, re-review 1; D-2511 seats != any writer family.
Writers D-2683/2684: agy gemini > mimo-pro > OR free/opencode free (~01Z) > mimo-flash; never Claude; cx out ~10-30. agy -p never a seat (L24). D-2686: no run here while MemAvailable < 4000 MB (incl. projected RSS; qwen seat ~880 MB) -> workst-L1-autoos.
Spawn: python3 ~/code/AutoOS/tools/autoos-agent.py run --isolate --client agy --model gemini-3.8-flash | --client opencode --model mimo/mimo-v2.6-pro --card '{"complexity":"standard","privacy":"public","role":"implement"}' --item-key K --title T "$(cat brief)" (cwd = lane worktree). Briefs/seats: ~/code/plangraph-worktrees/briefs/lanes-d2657{,/seats}.
Handoff = status/build-L1.handoff.md. Control repo local only: git push control build-L1.
| Item | State / next |
|---|---|
| #164 RT-OVERSIGHT-KEEPER-SCOPE (HIGH) | r9 88bc89d4 pushed (2711 keeper pass, gates 0). Delta seats: Haiku FAIL (seats/scope-r9.haiku.txt: 2 MEDIUM expired-auth audit race + lockout of live principal, LOWs; guard alarm first-wins not fixed); dsfree seat running (bqitts10b). Next: ONE r10 brief with all findings = round 3 of 3 (limit, then replan) -> writer via workst (D-2686) -> delta seat -> GO-REQ naming round |
| #183 ROUTER-LIVENESS-WATCHDOG | r2 ee2152fb pushed, CI running. qwen delta: all MED/LOW fixed; fail only on by-design HIGH + 2 cosmetic LOWs -> DECISION-REQ to routing (scope ruling, LOWs follow-up). Then MERGE-READY -> mergepath 183 |
| #184 HARDENING (3 keys) | CI fix acf975ea pushed (Gate-Edit D-2692, 28 pass), CI running. Haiku seat via workst-L1-autoos. Then mergepath -> evidence TEST-SPEED-AFFECTED + RUN-EXIT-NOTIFY, submit 3, prune hb-keys |
| RT-P1-EXEC-OWNERSHIP (HIGH) | D-2691 Haiku seat delegated to workst-L1-autoos (05:5xZ); then request close |
| PG-L3-MAX-TOKENS (D-2694/2695) | claimed e1; lane/l3-max-tokens pushed; writer delegated to workst-L1-autoos; then 1 seat -> mergepath |
| D-2691 closed 6 | KG-LINK, LESSONS-TO-ALGO, SEAT-VERDICT-QODER, WAIT-GUARD, SEAT-FAMILY-WRITER, PG-PUBLIC-READY |
| #166 KEEPER-PG-RECONNECT | after #164 |
| #139 KEEPER-TRAILER-MULTI | after #166 |
| KEEPER-WATCHDOG-ROLE (HIGH) | after #164; spec watchdog-principal.md; schema vs build-L1-bus |
| PG public cut | OS-PG-PUBLIC-PUSH operator; re-cut on "push window now" (pg-public-cut-plan.md) |
| LIVE-SYNC-9 | live 2d61e281; OS-KEEPER-LS9-MIGRATE operator, then z8 drop-in |
| L2s | build-L2-review-method d3c8d22a running (watch MERGE-READY); build-L1-bus peer |
Traps: zsh -> bash -c; mergepath needs claim, no submit; no xdist, affected tests only; never dump environ; Haiku seat = claude -p --model haiku --tools "" --strict-mcp-config --mcp-config jobs/975daff2/tmp/nomcp.json from tmp/empty (stdin).
