# Card sandbox admission

`card.sandbox` is admission data. The spawner grants one sandbox, `workspace-write`; a card cannot ask for more.

| `card.sandbox` | Result |
|---|---|
| absent, `null`, `workspace-write` | admitted |
| `danger-full-access` | `sandbox_refused: danger-full-access is never granted to a spawned run; only workspace-write is granted` |
| any other value (unknown string, number, list, object) | `sandbox_refused: card.sandbox is not a supported value; only workspace-write is granted` |

The refusal never echoes the card value. Python (`refuse_card_sandbox` in `planner/route_policy.py`) and Rust (`card_sandbox::refuse` in `pg-spawnd`) hold the same table; both are tested against the same cases.

## Where it is checked, in order

The check runs before any placement, quota, harness or binary probe, run-tree lease, supervisor start or forwarding:

- Python: `cli spawn`, `SpawnService._route`, `plan_spawn` (before `installed` runs), `SeatSpawnMixin.spawn`, `seat_spawn.admit`, `overflow.main`, `overflow.remote_spawn`, `overflow.native_spawn`, and `launch_spec` (`build` and `fresh_main`, before the run tree).
- Rust: `spawn::spawn` and `placement::route` (before explicit leg references reach the catalog), `placement::native_spawn`, and the native writer: `native-writer` refuses right after the supervisor guard, before the work disk is chosen, and records a terminal `refused` run (exit 5) through the lifecycle journal; `Host::configure` refuses the same card; `writer-resume` re-checks the saved request under the supervisor guard before `take_resume`, so a refused resume changes neither the run record nor `landing-stop.json`.

Fleet session flags (`--permission-mode bypassPermissions`) belong to the launcher, not to cards; the fleet-launch fixture pins the effective arguments of local and forwarded launches.
