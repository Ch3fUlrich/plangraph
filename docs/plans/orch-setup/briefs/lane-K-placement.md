# Lane K: placement rules and API (plangraph-L2-fc-placement), HIGH tier

STANDING RULES: `docs/plans/orch-setup/briefs/COMMON.md` (binding, read first). Design sections: 12.2-12.4.
Items, in order: `plangraph:PLACEMENT-RULES`, `plangraph:PLACEMENT-ENGINE`, `autoos:SPAWN-USES-PLACEMENT`.
- PLACEMENT-RULES starts after lane J's HOST-SAMPLES-API and lane A's SU-ENV.
- PLACEMENT-ENGINE waits for J's session and gateway signals.
- SPAWN-USES-PLACEMENT waits for lane C's SU-MCP and SU-LAUNCH.

## READ
- AutoOS spawner admission: the cap of 6 live workers, the `MemAvailable` floor, the flock claim, `slot_check.sh`, and the
  `AUTOOS_WORKERS_DIR` handling. Find them with Serena (`find_symbol` admission).
- The done-criteria of the four folded items: `autoos:T2-BUDGET`, `autoos:WS-4`, `autoos:AO-SLOT-GRANT-TOKEN`,
  `autoos:AO-ADMISSION-OC-SHARED`. Read them from the keeper (cancelled rows keep their text). They are acceptance tests
  here.
- D-081 / D-1485 (memory floors). Ask workst-L1-autoos for the text if you cannot read the decision log.
- Lane J's sample schema and `setup/env.schema.toml`.

## WRITE SCOPE
- plangraph:
  - `setup/placement-rules.schema.toml`;
  - a keeper `placement` module + migration;
  - routes: `POST /placement`, lease renew/release, `GET/PUT /placement/rules`, `GET /placement/decisions`;
  - the Rust env-loader extension for `PG_RULE_*`;
  - tests.
- AutoOS: the spawn path (CLI + MCP) calls the API; local admission is kept only as the offline fallback; tests.

## Spec
- **Rules** (design 12.2 table): HostFresh, HostWorkerCap, HostMemoryFloor, HostGpuFit, ProviderConcurrencyCap,
  ProviderCooldown and WorkstationUsableGuard (filters); HostLoadScore and HostPreferenceScore (scores); SlotLease (grant);
  OverflowQueue (queue). One check per name; the names are fixed.
- **Schema:**
  - fields: name, type, unit, range, default, `host_overridable`;
  - env keys are derived as `PG_RULE_<SNAKE_NAME>_<UNIT>`, with units `_COUNT`, `_MIB`, `_PCT`, `_S`, `_ENABLED`;
  - there is no second list (test: schema == env.schema keys == UI form fields).
- **Precedence:**
  - order: process env > env file > keeper > schema default;
  - only `host_overridable` keys may be set per host;
  - a bad key is skipped for that key only and reported; only an unparsable file refuses.
- **Keeper edits:**
  - append-only events: who, when, why, before/after, version;
  - a rollback is a forward event;
  - hosts report overrides with every sample;
  - hosts keep a last-known-good copy on disk; a keeper outage means last-known-good, else defaults flagged `unmanaged`.
- **Engine:**
  - filter, then score;
  - effective memory = `MemAvailable` minus the estimates of granted-but-unstarted jobs;
  - hysteresis: two thresholds + 2 consecutive samples, EWMA half-life of about 5 min;
  - ProviderCooldown is fleet-wide (no per-host penalty);
  - SlotLease: TTL + heartbeat renewal, expiry frees the slot, single use;
  - OverflowQueue: FIFO within priority; timeout 1800 s, then fail with reasons;
  - every decision is recorded with each host's failed filter or score.
- **Workstation:** eligible within its own caps (WS-4: 80 GB worker RAM budget); workers start at below-normal priority; no
  idle gate (S23).

Checks:
- replay tests on recorded samples:
  - no double-booking between samples;
  - hysteresis holds;
  - queue + timeout;
  - lease expiry;
  - forged/reused/absent lease refused;
  - decision p95 < 50 ms;
- precedence test across the 4 layers;
- audit + rollback test;
- AutoOS CLI + MCP spawn hold a lease;
- offline fallback test;
- the 4 folded done-criteria pass;
- 2 non-Claude seats.

## Return
DONE per item: branch, head sha, check output tail, writer model(s), seat verdicts.
