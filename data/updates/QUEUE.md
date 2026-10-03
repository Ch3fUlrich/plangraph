# Load queue for the pilot keeper (router-confirmed order; each needs routing-00's OK on the live diff)
Prerequisite: KEEPER-BATCH merged (apply_ops; B3 keeps extra fields). Each batch: trial on a DB copy -> diff + `pg ready --all --brief`
to routing-00 -> OK -> `tools/apply_batch.py FILE --url http://127.0.0.1:8787 --token-file ~/.config/plangraph/router.token`.

| # | batch | file | status |
|---|---|---|---|
| 0 | plan-v3 import (D-314 fold) | data/plan-v3 | LOADED 2026-10-02 |
| 1 | router 2026-10-01 A-C (76 ops) | data/updates/2026-10-01-router.yaml | LOADED 2026-10-02 |
| 2 | server b1 (81 ops @fae4f5b) + router GOAL link (data/updates/2026-10-02-router-goal-link-server.yaml) | Server t6/server-plan docs/plan/graph/2026-10-02-server-plan.yaml | LOADED 2026-10-02 |
| 3 | server d298 | ...-server-plan-d298.yaml | LOADED 2026-10-02 |
| 4 | server backlog v2.1 | ...-server-plan-backlog.yaml | LOADED 2026-10-02 |
| 5 | server plans | ...-server-plan-plans.yaml | LOADED 2026-10-02 |
| 6 | server d311 | ...-server-plan-d311.yaml | LOADED 2026-10-02 |
| 7 | server d336 | ...-server-plan-d336.yaml | router-OK'd (D-340) |
| 8 | server d342 | ...-server-plan-d342.yaml | LOADED 2026-10-02 |
| 9 | server d347 (batch 8) | ...-server-plan-d347.yaml | LOADED 2026-10-02 |
| 9b | server d352 (batch 9) | ...-server-plan-d352.yaml | LOADED 2026-10-02 |
| 10 | router closes 2026-10-02 (12 ops) | data/updates/2026-10-02-router-closes.yaml | LOADED 2026-10-02 |
| 11 | plangraph M2c plan (29 items) | data/updates/2026-10-02-plangraph-m2c.yaml | LOADED 2026-10-02 |
Server files: take the latest commit of t6/server-plan at load time and re-run tools/plancheck.py chained before the trial.

D-367: bundle OK; re-trial on a fresh copy 2026-10-02 with server @fae4f5b: all 13 batches applied fully (366 items, verify ok).
Live apply waits only for the B1+B3 seats. Later: server d311-priority after KEEPER-BATCH B2 (priority op).

LIVE 2026-10-02 ~14:00Z (D-371): all 13 applied atomically on the pilot; 366 items, verify ok. Pending: server d311-priority after B2.
D-446 close RESEAT-MODEL-A (4 ops): data/updates/2026-10-02-router-close-d446.yaml — LIVE 2026-10-02 ~18:05Z (applied 4 of 4).
D-447 close autoos:EVTAG (4 ops): data/updates/2026-10-02-router-close-d447.yaml — LIVE 2026-10-02 (applied 4 of 4) after the operator allowance (OS-92).
D-413 rehome of the 10 OPEN workstation:* items -> autoos host=workstation: data/updates/2026-10-02-router-rehome-workstation.yaml — LIVE 2026-10-02 (applied 10 of 10, verify ok, aliases recorded) on apply_batch rehome (8af0e23; seats gpt-oss PASS review-call 4a208af6 + Qwen3.5-397B PASS 943c31bd). D-428 closes: LIVE (above).
D-459 close server:T-016 (4 ops): data/updates/2026-10-02-router-close-d459.yaml — LIVE 2026-10-02 (applied 4 of 4, verify ok). Rehome batch content router-OK'd (D-459 msg); applies after the apply_batch rehome fix merges.
D-420 close server:T-023 (4 ops): data/updates/2026-10-02-router-close-d420.yaml — LIVE 2026-10-02.
D-428 closes autoos:T3-CIGREEN + T3-CIMAIN (6 ops): data/updates/2026-10-02-router-closes-d428.yaml — LIVE 2026-10-02 ~19:00Z on the D-434 tier-edit code (applied 6 of 6, verify ok).
Daemon restarted ~19:00Z on worktree code = 610271d (integ + mask + tier-edit; DB backup keeper.db.pre-modela); `pg migrate-descriptions`: migrated 76, skipped 0.
md-sync timer had halted 18:55Z (old daemon); re-run OK on new code (0 ops second run), timer + path re-armed.
Server 2026-10-03-server-plan-d459 (6bda845b, 7 ops; source staged from t6/server-plan, sha d392fc3e): router OK; LIVE 2026-10-02 (applied 7 of 7, verify ok) after the operator widened OS-92 to plan batches.
D-464 close server:T-020 (4 ops): data/updates/2026-10-02-router-close-d464.yaml — LIVE 2026-10-02 (verify ok).
D-465 close server:T-004 (4 ops): data/updates/2026-10-02-router-close-d465.yaml — LIVE 2026-10-02 (verify ok).
Audit gaps (router-OK, operator-instruction audit 2026-10-02): data/updates/2026-10-02-router-audit-gaps.yaml sha 4c7eedb4… — LIVE (create 28 = 5 Q + 22 gap rows + gate routing:PH-AUDIT-1002; routing:GOAL 52→53; 7 priorities; verify ok). First apply refused by the credential guard on 'explo/audiomuse' (slash pair near 'login'-like context) → rephrased.
md-sync closes pre-check + status file + multi-id parser: merged cf8e6af (=750a9e5; seats gpt-oss review-call cc98b17b PASS + Qwen 69f9bc00 PASS; 1043 green WSL). Service now writes ~/.local/share/plangraph/md-sync.status.json. First run 23:41Z applied 19 ops: OS-60/72/85/86/90/94 done; second run 0 ops; verify ok.
D-502..D-504 closes (7 items, 28 ops): data/updates/2026-10-03-router-closes-d502-d504.yaml — LIVE 2026-10-03 ~03:50Z (verify ok).
D-492..D-524 progress (29 items + 3 gates + GOAL edit + 4 state ops = 7 ops): data/updates/2026-10-03-router-progress-d492-d524.yaml — TRIAL OK on a DB copy 2026-10-03 (applied 7 of 7, verify ok, 489 -> 521 items); waits for routing's OK on the dry run; source sha pinned to decisions-log 6785794d… (re-pin if the log changes before apply). Server lanes + their in_review ops: follow-up batch after server-L1's plan batch.
Server new lanes (server-L1 draft, 19 lanes + gate routing:PH-SERVER-1003 + GOAL edit + 5 in_review for D-516/D-519 Closes): data/updates/2026-10-03-server-plan-new-lanes.yaml — TRIAL OK on a DB copy (applied 26 of 26, verify ok, 521 -> 541 items); LIVE 2026-10-03 ~09:23Z (applied 26 of 26, verify ok, head 00418362643e, 541 items; backup keeper.db.pre-serverlanes).
D-492..D-526 progress batch (routing OK D-526, state corrections): LIVE 2026-10-03 ~09:21Z (applied 32 of 32, verify ok, 489 -> 521 items; backup keeper.db.pre-progress). GEN-CLOSEOUT stays in_review until K1 is merged (the live parser rejects key@sha tokens).
D-526 close workst-gen:GEN-CLOSEOUT (2 ops): data/updates/2026-10-03-router-close-d526.yaml — LIVE 2026-10-03 ~11:35Z on the K1 code (applied 2 of 2, verify ok). K1 (9f97b37) merged to worktree-proposal as 5b4fe9d..8cdad08 on router OK D-537; keeper config repos = per-repo refs (server/autoos/routing origin/main, plangraph main); daemon restarted ~11:32Z (backups keeper.db.pre-k1, keeper.json.pre-k1).
