# Lane F: keeper lease config (plangraph-L2-su-lease), HIGH tier

STANDING RULES: `docs/plans/orch-setup/briefs/COMMON.md` (binding, read first). Design sections: 3 (heartbeat), 6 (Keeper lease
config).
Item: `plangraph:SU-LEASE-CFG`. Independent; it can start right after approval (only SU-DOCS before it).

## READ
- plangraph `src/plangraph/keeper/config.py:94-97,146-153` (parsed keys).
- `src/plangraph/keeper/store.py:50-52,86-88` (hard-coded constants).
- `src/plangraph/keeper/store_verbs.py:92,159-189` (claim lease, heartbeat).
- `src/plangraph/keeper/api.py:347-352` (reaper), and the Postgres backend's equivalent (`PG_KEEPER_BACKEND=postgres`).

## WRITE SCOPE
Only the keeper modules above plus `tests/test_keeper_lease_cfg.py` and a short `docs/KEEPER.md` note. Nothing else.

## Spec
- `default_lease`, `lease_warn_s`, `review_overdue_s` (and `reap_interval` if it is also ignored) from keeper.json reach the
  Store on both backends. Defaults are unchanged when the keys are absent.
- `PG_LEASE_S` / `PG_HEARTBEAT_S` (env file) are client-side values documented next to them.
- Checks:
  - a keeper.json with `default_lease: 600` gives a new claim `lease_until = now + 600`;
  - heartbeat extends by the configured value;
  - absent keys behave as today;
  - the full suite is green on both CI jobs (SQLite and Postgres 16).
- The keeper is live. Do not restart or touch any running keeper; tests only.
