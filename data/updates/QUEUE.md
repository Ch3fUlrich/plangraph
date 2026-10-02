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
DRAFTS (not applied): 2026-10-02-router-rehome-workstation.yaml (10 rehome ops; needs apply_batch rehome op), 2026-10-02-router-closes-d428.yaml (6 ops; needs the D-434 tier-edit code live).
D-459 close server:T-016 (4 ops): data/updates/2026-10-02-router-close-d459.yaml — LIVE 2026-10-02 (applied 4 of 4, verify ok). Rehome batch content router-OK'd (D-459 msg); applies after the apply_batch rehome fix merges.
D-420 close server:T-023 (4 ops): data/updates/2026-10-02-router-close-d420.yaml — LIVE 2026-10-02.
D-428 closes autoos:T3-CIGREEN + T3-CIMAIN (6 ops): data/updates/2026-10-02-router-closes-d428.yaml — LIVE 2026-10-02 ~19:00Z on the D-434 tier-edit code (applied 6 of 6, verify ok).
Daemon restarted ~19:00Z on worktree code = 610271d (integ + mask + tier-edit; DB backup keeper.db.pre-modela); `pg migrate-descriptions`: migrated 76, skipped 0.
md-sync timer had halted 18:55Z (old daemon); re-run OK on new code (0 ops second run), timer + path re-armed.
Server 2026-10-03-server-plan-d459 (6bda845b, 7 ops; source staged from t6/server-plan, sha d392fc3e): router OK; LIVE 2026-10-02 (applied 7 of 7, verify ok) after the operator widened OS-92 to plan batches.
D-464 close server:T-020 (4 ops): data/updates/2026-10-02-router-close-d464.yaml — LIVE 2026-10-02 (verify ok).
D-465 close server:T-004 (4 ops): data/updates/2026-10-02-router-close-d465.yaml — LIVE 2026-10-02 (verify ok).
