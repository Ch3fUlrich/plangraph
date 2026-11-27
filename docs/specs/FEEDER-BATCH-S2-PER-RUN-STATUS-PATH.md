# FEEDER-BATCH-S2-PER-RUN-STATUS-PATH

Outcome. Give each feeder batch its own review status file and launch related ready items together under the six-item default.

- plan_source. Keeper card FEEDER-BATCH-S2-PER-RUN-STATUS-PATH, the FEEDER-BATCH-S1-CLAIM-RELEASE-EPOCHS prerequisite and the batch rollout brief.
- base commit. d948affa1da9565f12c58fb981bbf2dbc04974ad
- implementation commit. 4614ba041
- card SHA-256. 57519fbc6009839bfbd3cdc70f793875ba75857c2d76d5b8ba33184af3bf6a11
- tree SHA-256. 8d55c7404bdf5cc5b5f19b45738d54f3fceffe426da22b65dfe67e7e324fec59
- evidence_after_deploy. Deploy the CLI and shipped policy under the feeder rollout GO. Record the policy value batch_max_items = 6 in that GO and observe a multi-item launch on the approved host.

## Decisions

- D1. Concurrent writers shared one status file, mixing DONE lines in their review briefs. A one-item default also prevented the shipped policy from exercising the batching path.
- D2. Reserve a unique live status path in the review directory and pass it through the card and batch payload. Native writer persistence carries the card path into writer_status_path, and the review renderer uses that field. Policy construction and the shipped policy both default to six items.

## Entry points

- E1. Feeder tick and live launch adapter in rust/plangraph-cli/src/feeder/workflow.rs and adapters.rs.
- E2. Native writer record and versioned review brief renderer.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | Two concurrent batches receive distinct paths and each batch card matches its brief. | X1 |
| B2 | E2 | Two saved writer records point to their own files. Each review quotes only that writer's DONE lines. | X2 |
| B3 | E1 | Two related ready items launch together with the built-in, empty TOML and shipped policies. | X3 |

## Verification

- X1. feeder_batches::overlapping_feeder_launches_pass_distinct_status_paths_to_the_card_and_brief and feeder::adapters::tests::status_files_are_reserved_exclusively.
- X2. native_writer_host::review_tests::reviews_rendered_from_saved_writer_records_quote_only_their_own_done_lines.
- X3. feeder_batches::related_ready_items_launch_one_batch_under_each_default_policy.

## Scope and reuse

- R1. Reuse the existing claim epochs, batch brief renderer and native writer record fields. The feeder service resolves deploy/feeder/policy.toml from its working directory.

## Open points for design approval

None.
