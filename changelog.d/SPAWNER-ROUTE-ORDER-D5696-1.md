- D-5696/D-5706 slice 1: existing `payment_order` owns implement, review classes
  and planning order, writer fences, legacy refusals, Haiku sizing and Ember votes.
- Round 2 removes the parallel `OPERATOR_ROUTE_CATALOG`: fixed order consumes
  existing `DEFAULT_LEGS`/explicit registrations. New identities live in
  `DEFAULT_LEGS`, permanently disabled with empty roles and unset capabilities
  until measured evidence exists. Existing capability facts are retained, not cloned.
- D-5712: implement Sonnet high never uses the Claude Code subscription. The
  core names `D-5712: claude_code_subscription_forbidden_for_implement` on refusal,
  admits AgY (`agy-sonnet-5.5`) or non-Claude-Code high Sonnet, and advances from
  position 2 to 4 when position 3 has no admitted source.
- Position 8 is **dead**: PR #818's recorded listing serves neither old
  `omniroute/deepseek-flash` nor corrected `omniroute/deepseek-v4.1-flash`.
  `planner/legs.py:129` retains the old id: operator's **report dead** option,
  not a claimed id fix. Launch refuses both; no host evidence is fabricated.
  The distinct served OpenCode Go identity stays `opencode-go/deepseek-v4.1-flash`.
  Probe filtering and no-child-start regressions are hermetic, not `real_probe`.
- Live activation, legacy fencing, policy/role migration and utility resolver
  replacement remain out of scope. Probes and host deployment remain L1-owned.
- **D-5696 stays OPEN, not DONE.** `test_live_legacy_door_keeps_d5696_open`
  demonstrates the unfenced live legacy door at `planner/routing.py:306,440`.
  `test_implement_order_efforts_and_limited_fallthrough` proves live D-3828
  still excludes Sonnet: the one-leg result is not trimming. The core admits both.
  `test_position_8_is_dead_until_host_evidence` pins the dead default leg.
  No new benchmark, waiver, score or publisher evidence is invented.
