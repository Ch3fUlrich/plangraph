Keeper startup publishes loaded SHA/process identity; the fleet-sync main-ff command holds updates changing existing keeper/imported code, with hourly templates and HELD status on the existing local drift surface. Unused additions and unrelated spawner changes remain eligible.
- Recheck keeper state before main updates; hold for live units without matching startup state, track checkout provenance and guard the three traced runtime data files.
- Distinguish absent keeper units from failed queries, inspect both systemd scopes and standalone processes, and report fixed liveness-probe causes on holds.
- Detect keeper file/module launch targets only in interpreter processes, remove the pgrep dependency, prefer actionable live-keeper holds, and cover inconclusive probe failure paths.
