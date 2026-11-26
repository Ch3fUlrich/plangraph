# ROUTE-ORDER-D6804

Outcome. Prefer Antigravity Sonnet high for the Claude-family review seat and use the Claude subscription when its fallback condition is met.

- plan_source. Keeper card plangraph:ROUTE-ORDER-D6804, D-6952 and D-6953, with D-6957 and D-6965.
- base commit. d948affa1da9565f12c58fb981bbf2dbc04974ad
- card SHA-256. 351070979f8d474d22be50ba4a5a0a68d465614f01b08a4baff370de52e8a796
- tree SHA-256. da6d8f530e616e6e8412210972ab175b6406d9bb39ede94e8319816c9db2e23c
- evidence_after_deploy. After deploy, record a role-review route dry run on the placed host showing Antigravity Sonnet high ahead of the Claude subscription.

## Decisions

- D1. D-6952 puts Antigravity Sonnet high first in the Claude-family review seat.
- D2. D-6896 keeps native Codex first for writing. D-6957 places Antigravity Gemini Neon medium on cost hold for reviews.
- D3. D-6965 keeps the native evidence fixture independent of the shared Cargo cache lease.

## Entry points

- E1. `REVIEW_ORDER` and `plan_spawn` in `src/plangraph/planner/` and host admission through the launch resolver.
- E2. `rust/pg-spawnd/src/cli_review.rs` native review seat selection.
- E3. `deploy/systemd/plangraph-review-probe.service` scheduled liveness probes.
- E4. `rust/pg-spawnd/tests/native_evidence.rs` native annotation verification.

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | A Claude-family review chooses Antigravity Sonnet high first. A limit on its quota or a missing Antigravity client admits the subscription fallback. | X1 |
| I1 | E1 E2 | Native Codex keeps its writer priority and review routing and selection exclude Neon medium on cost hold. | X2 |
| S1 | E1 E3 | Host admission reports a missing Claude executable for the subscription fallback and the unit allows all configured sequential review probes to finish. | X3 |
| S2 | E4 | Native evidence checks run annotations independently of the Cargo cache lease. | X4 |

## Verification

- X1. E1, changed. `test_review_route_agy_sonnet_before_subscription`, `test_review_route_subscription_when_agy_sonnet_limited`, and `test_review_route_subscription_when_agy_client_missing`.
- X2. E1 E2, changed. `test_implement_route_unchanged_gpt_first`, `test_review_route_excludes_neon_on_operator_cost_hold`, and `agy_cli_selector_refuses_a_stale_neon_route`.
- X3. E1 E3, changed. Claude host admission, benchmark route ordering, and review-probe unit tests.
- X4. E4, changed. Native evidence integration tests.

## Scope and reuse

- R1. Reuse the existing review order, seat coverage, host eligibility, and independent Antigravity Sonnet quota.
- R2. Keep the scheduled probe's existing per-leg timeout and allow the complete sequential budget at the service level.
