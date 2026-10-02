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
