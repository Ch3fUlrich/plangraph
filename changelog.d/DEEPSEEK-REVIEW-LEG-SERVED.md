# DEEPSEEK-REVIEW-LEG-SERVED (D-5641, D-5643)

- Cause: `src/plangraph/planner/legs.py:DEFAULT_LEGS` registers
  `omniroute-deepseek-flash` as `omniroute/deepseek-flash`, while routing reported
  `omniroute/deepseek-v4.1-flash`. The spawner's
  `src/plangraph/routing_support/resolver.py:_probe_served_models` calls
  `opencode models --standalone` with the child provider overlay;
  `host_skip_reason` returns `not_served_on_host: <model>` for an absent id, and
  `plan_for_launch` filters it before a child run starts.
- Evidence: local record `run-cb082cf0` contains
  `not_served_on_host: omniroute/deepseek-flash`. Routing reported the same reason
  for `run-c61d1906`, `run-414839b6`, and `run-c0780cac`; those three records are
  not present locally and were not independently verified.
- Read-only discovery on 2026-11-13 contradicts the reported target availability:
  neither `omniroute/deepseek-flash` nor `omniroute/deepseek-v4.1-flash` is listed.
  The only listed OmniRoute DeepSeek id is `omniroute/deepseek-direct-flash`,
  whose provider entry targets `deepseek/deepseek-flash`; it is a distinct paid
  route, not a substitute for this subscription review leg.
  `opencode-go/deepseek-v4.1-flash` is listed and already matches
  `opencode-go-deepseek-v4-flash`, so that row needs no correction.
- Correct the stale subscription registry id to the routing-reported
  `omniroute/deepseek-v4.1-flash` and update registry projections without changing
  its roles, tier, or any other leg. This corrects the name, not host readiness:
  the host operator must declare/serve this id and verify its gateway and
  credential path. The shipped provider config now declares model key
  `deepseek-v4.1-flash` under provider `omniroute`, exposing
  `omniroute/deepseek-v4.1-flash`; its unprefixed `modelID` is the gateway
  backend name. Credentials are unchanged.
- Replay the recorded DeepSeek-only probe output in regression tests: both
  absent OmniRoute spellings are skipped with `not_served_on_host`, the listed
  Go id is offered, and an unserved review leg cannot start a child process.
