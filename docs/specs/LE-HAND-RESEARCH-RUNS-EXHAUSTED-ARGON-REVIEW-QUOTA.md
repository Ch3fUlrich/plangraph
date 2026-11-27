# LE-HAND-RESEARCH-RUNS-EXHAUSTED-ARGON-REVIEW-QUOTA

Outcome. Share one Antigravity Gemini quota between Argon and Hydrogen and skip either model on review and research routes while the pool is limited.

- plan_source. Keeper card plangraph:LE-HAND-RESEARCH-RUNS-EXHAUSTED-ARGON-REVIEW-QUOTA, D-6952 and D-6953.
- base commit. d948affa1da9565f12c58fb981bbf2dbc04974ad
- card SHA-256. a57a4b23c5814e6885a8983086f15b516e095143911c6e5c1b001bdec8937b29
- tree SHA-256. dff0ea065a2172f94321cb25d59dc38a78d4a05b92f32420ddc6654b82dcf13a
- evidence_after_deploy. After deploy, record a route dry run showing the same booked quota and reset for both model keys and a limited skip on the affected route.

## Decisions

- D1. Research drained the shared quota used by review seats. D-6952 establishes that both models draw on the same allowance.
- D2. Keep `agy:gemini-4-hydrogen` as the canonical key and read historical bookings for both models across existing separators and effort suffixes.

## Entry points

- E1. `src/plangraph/routing_support/leg_state.py` quota normalization and leg status.
- E2. `src/plangraph/planner/routing.py` review and research routing and quota diagnostics.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | Either model's 429 limits both models until reset. Neon and Sonnet keep separate quota keys. | X1 |
| I1 | E1 | Historical Argon bookings still limit the pool. Probe-down state stays specific to the probed model. | X2 |
| S1 | E2 | Review and research see the same quota status under either Gemini key and skip a limited leg. | X3 |

## Verification

- X1. E1, changed. `test_quota_key_gemini_pool`, `test_argon_429_limits_hydrogen`, and `test_hydrogen_429_limits_argon`.
- X2. E1, changed. `test_pre_pool_argon_booking_counts`, `test_overlapping_pre_pool_bookings_hold_until_both_reset`, and `test_gemini_probe_failure_stays_local_but_recovery_clears_the_pool`.
- X3. E2, changed. `test_agy_quota_states_share_the_gemini_pool`.

## Scope and reuse

- R1. Reuse the existing quota ledger, account scope, reset handling, and route skip reasons.
- Follow-up under D-6953. Review reserve and token-budget stop, remaining quota display, and the planning skill quota read belong to the second batch on this keeper card.
