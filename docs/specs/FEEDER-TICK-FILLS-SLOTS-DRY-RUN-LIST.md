# FEEDER-TICK-FILLS-SLOTS-DRY-RUN-LIST

Outcome. Use one feeder tick loop to fill available host slots and list the same eligible batches in dry-run mode.

- plan_source. Keeper card FEEDER-TICK-FILLS-SLOTS-DRY-RUN-LIST and D-6826, slice 3 of the FEEDER-LIVE-ALL-HOSTS replan.
- base commit. d948affa1da9565f12c58fb981bbf2dbc04974ad
- implementation commit. f9a83a627
- card SHA-256. fb160e1b80d4642eb5a248ebbf9efe5d38a4b3053f8003ccb9a107524511910f
- tree SHA-256. b468de5255289ead827701a3529bf87714a321a688c5985f6ae454b9a83167b7
- evidence_after_deploy. Deploy the rebuilt CLI through the feeder rollout named in the changelog.

## Decisions

- D1. A tick that stops after one batch leaves spare host capacity unused and gives an incomplete dry-run preview.
- D2. After each batch, reserve one writer slot, reduce available RAM and remember the batch's files. Recheck admission before the next selection. Apply the batch limit during selection. On a later launch failure, retain earlier accepted starts and record the stop reason.

## Entry points

- E1. Feeder tick in rust/plangraph-cli/src/feeder/workflow.rs.
- E2. The dry-run would_start list and live started list returned by the feeder.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 E2 | A three-slot fixture lists three disjoint dry-run batches and starts the same three live batches. An overlapping item waits. | X1 |
| B2 | E1 | RAM limits the number of proposed batches after subtracting one run's allocation per slot. | X2 |
| B3 | E2 | A later live start failure records its reason and preserves the earlier successful start. | X3 |

## Verification

- X1. feeder_slots::dry_run_lists_every_batch_the_live_tick_starts_and_defers_overlapping_work.
- X2. feeder_slots::each_dry_run_batch_consumes_one_runs_ram.
- X3. feeder_slots::a_later_launch_failure_stops_the_tick_but_keeps_its_successful_start.

## Scope and reuse

- R1. Reuse the existing claim and start result contracts and preserve the first decision fields. Deadline accounting is a separate replan slice.

## Open points for design approval

None.
