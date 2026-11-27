# FEEDER-QUOTA-CENTER-SNAPSHOT-READ

Outcome. Read fresh center quota on live workers without local Codex observations and retain capacity diagnostics on stale-quota refusals.

- plan_source. Keeper card FEEDER-QUOTA-CENTER-SNAPSHOT-READ and D-6826, slice 5 of the FEEDER-LIVE-ALL-HOSTS replan.
- base commit. d948affa1da9565f12c58fb981bbf2dbc04974ad
- implementation commit. d174689f7
- card SHA-256. a442496e252c62b64bca403660b542e8b7d909b56568694a70f2aace7263ce21
- tree SHA-256. 3d586f6e9799b792b2dca1cc549a37fa3928c39b5a146506e0578935fcd023cf
- evidence_after_deploy. Deploy the rebuilt CLI and policy under the feeder rollout GO. Provide the center snapshot at the configured path before enabling worker timers.

## Decisions

- D1. Workers without local quota observations need an admission input. A fresh envelope alone does not establish that the observations are usable.
- D2. Keep local Codex observations authoritative when present. Otherwise read the center file and apply both the snapshot envelope and per-observation age limits. Report both ages when available and keep the live capacity line, including writer count and RAM reservations, when quota is stale.

## Entry points

- E1. Center quota reader and facts outcome in rust/plangraph-cli/src/feeder/quota.rs and sources.rs.
- E2. Live feeder decision in rust/plangraph-cli/src/feeder/runtime.rs.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A worker without local rows uses a fresh center snapshot. A host with local Codex observations keeps its local reader. | X1 |
| B2 | E1 E2 | Missing, malformed, expired or unusable snapshots refuse as quota_stale with the two ages or unknown values. | X2 |
| B3 | E2 | A stale-quota decision retains the writer count and RAM reservation reported by a fresh tick. | X3 |

## Verification

- X1. a_host_without_codex_rows_reads_the_center_file_and_never_gets_null_quota and existing account isolation tests.
- X2. center_snapshot_is_used_while_fresh_and_names_its_age_when_stale and a_fresh_envelope_around_old_or_empty_observations_is_stale_quota_with_ages.
- X3. a_stale_quota_refusal_keeps_the_writers_and_ram_reservation_of_a_fresh_tick and center_quota_drives_live_admission_and_preserves_capacity_when_stale.

## Scope and reuse

- R1. Reuse the current account and window evaluation. Snapshot publishing and delivery are separate work.

## Open points for design approval

None.
