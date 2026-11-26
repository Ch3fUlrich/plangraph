Antigravity Gemini Argon and Hydrogen share one quota (D-6952). Review and research routing honor bookings for either model, including existing Argon rows, under the Hydrogen quota key. Model-specific probe failures, account scope, and the Neon and Sonnet quotas stay independent.

Tests cover the shared key, bookings, and routing.

- `test_quota_key_gemini_pool`
- `test_argon_429_limits_hydrogen`
- `test_hydrogen_429_limits_argon`
- `test_pre_pool_argon_booking_counts`
- `test_overlapping_pre_pool_bookings_hold_until_both_reset`
- `test_gemini_probe_failure_stays_local_but_recovery_clears_the_pool`
- `test_agy_quota_states_share_the_gemini_pool`

`LE-HAND-RESEARCH-RUNS-EXHAUSTED-ARGON-REVIEW-QUOTA` stays open for the second batch covering the review reserve and token-budget stop.
