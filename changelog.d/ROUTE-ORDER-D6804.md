Use Antigravity Sonnet high for the Claude-family review seat before the Claude subscription (D-6952). Subscription fallback requires an Antigravity Sonnet quota limit or a missing Antigravity client. Native Codex keeps its writer priority.

D-6957 places Antigravity Gemini Neon medium on operator cost hold in review routing and CLI seat selection.

The native evidence test checks run annotations without waiting for a Cargo cache lease held by fast checks.

Tests cover reviewer selection, subscription fallback, and writer priority.

- `test_review_route_agy_sonnet_before_subscription`
- `test_review_route_subscription_when_agy_sonnet_limited`
- `test_review_route_subscription_when_agy_client_missing`
- `test_review_route_does_not_use_subscription_for_a_paused_agy_client`
- `test_implement_route_unchanged_gpt_first`
- `test_review_route_excludes_neon_on_operator_cost_hold`
- `agy_cli_selector_refuses_a_stale_neon_route`
