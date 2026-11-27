# FEEDER-HAND-LANES-COUNT-AND-HOLD

Outcome. Count each live fleet-launch writer once on its host and hold every item key on its card.

- plan_source. Keeper card FEEDER-HAND-LANES-COUNT-AND-HOLD and D-6826, slice 4 of the FEEDER-LIVE-ALL-HOSTS replan.
- base commit. d948affa1da9565f12c58fb981bbf2dbc04974ad
- implementation commit. f3e1f874f
- card SHA-256. 2e7da6a7ab1217a4cf4a793fd3c16309fcb4e782c607750a8673becc4ec30ca3
- tree SHA-256. 63523c56721a48bf1eaa90278e67a5429335e6a88db1ec67530aadc0374de2ef
- evidence_after_deploy. Deploy the rebuilt CLI and updated spawner package to feeder hosts and their remote ps producers.

## Decisions

- D1. A live hand lane can occupy a host slot and own multiple items before the feeder sees a matching spawner record.
- D2. Merge launcher sessions and spawner rows by run ID. PID, active Claude roster and fresh working job state provide independent liveness evidence. Preserve run_kind in remote ps so the reader can split whole item-key tokens from a registered fleet card before the launcher record arrives.

## Entry points

- E1. Launcher facts and run merge in rust/plangraph-cli/src/feeder/lanes.rs and sources.rs.
- E2. Remote ps projection in src/plangraph/spawner/remote_ps.py.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A live hand lane consumes one slot on its host and all keys on its card are ineligible. | X1 |
| B2 | E1 | A run listed by both sources is counted once. Malformed or missing job state supplies no liveness evidence. | X2 |
| B3 | E1 E2 | Remote ps retains run_kind, allowing the feeder to split the whole card and retain known item scope. | X3 |

## Verification

- X1. hand_lanes_count_as_host_writers_and_every_key_of_their_card_is_held and card_identity_and_unknown_scope_only_consume_their_host_slot.
- X2. lanes unit tests and a_session_both_listed_by_the_spawner_and_recorded_is_counted_once.
- X3. a_spawner_row_holding_the_whole_card_is_split_into_its_keys and test_fleet_session_rows_keep_run_kind_for_the_feeder_card_split.

## Scope and reuse

- R1. Reuse fleet-launch session records, existing live and terminal status semantics and the feeder's item and file overlap checks.

## Open points for design approval

None.
