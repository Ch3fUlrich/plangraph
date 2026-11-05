# Spawner prompt rules

`render_fixer_brief` in `src/plangraph/routing_support/fixer.py` numbers every
finding: each must be fixed, refuted by a named test, or reported as `not done:`
prose with the reason. Never echo an unchecked checklist or use a `Remaining gaps`
section for terminal `not done:` items. Run affected tests only
(`python3 tools/affected_tests.py --base origin/main --run`); COMMIT before finishing.

Retry bounds are not multiplied:
`src/plangraph/settings.py:ReviewMethod.max_rounds` (3) is advisory, not an enforced
run cap; `src/plangraph/planner/routing.py:ATTEMPT_BUDGET` (3) feeds
`next_after_failure`'s per-tier retry/promotion policy, but the spawner's
`src/plangraph/spawner/service.py:_spawn` walks `plan.legs` once (no numeric
fallthrough constant), stopping on a non-provider/non-unavailable result or
`no_fallthrough`. Thus leg exhaustion or that earlier stop ends this spawn first,
not the advisory round cap.
