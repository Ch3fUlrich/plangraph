- D-5696/D-5706 slice 1: existing `payment_order` owns implement, review classes
  and planning order, writer fences, legacy refusals, Haiku sizing and Ember votes.
- Round 2 removes the parallel `OPERATOR_ROUTE_CATALOG`: fixed order consumes
  existing `DEFAULT_LEGS`/explicit registrations. New identities live in
  `DEFAULT_LEGS`, permanently disabled with empty roles and unset capabilities
  until measured evidence exists. Existing capability facts are retained, not cloned.
- D-5712: implement Sonnet high never uses the Claude Code subscription. The
  core names `D-5712: claude_code_subscription_forbidden_for_implement` on refusal,
  uses the real explicit `agy-claude-sonnet-5-5-high` registration (no manufactured
  medium-leg alias), and advances from position 2 to 4 without an admitted source.
- Position 8 is **dead**: PR #818's recorded listing serves neither old
  `omniroute/deepseek-flash` nor corrected `omniroute/deepseek-v4.1-flash`.
  `planner/legs.py:129` retains the old id: operator's **report dead** option,
  not a claimed id fix. Launch refuses both; no host evidence is fabricated.
  The distinct served OpenCode Go identity stays `opencode-go/deepseek-v4.1-flash`.
  Probe filtering and no-child-start regressions are hermetic, not `real_probe`.
- Live fixed-order activation, legacy fencing, policy migration and utility resolver
  replacement remain out of scope. Probes and host deployment remain L1-owned.
- **D-5696 stays OPEN, not DONE.** `test_live_legacy_door_keeps_d5696_open`
  no longer asserts successful legacy routing. **Brief item 2 is UNMET:**
  `src/plangraph/planner/routing.py:306,440` still admit legacy models outside
  fixed ordering. A live fence trial made 83/223 bounded routing tests fail;
  updating that migration exceeds this last ~350-line fix. The named test now
  checks fixed-order refusal only, not a claim of live admission safety.
  `test_implement_order_efforts_and_limited_fallthrough` proves live D-3828
  still excludes Sonnet: the one-leg result is not trimming. The core admits both.
  `test_position_8_is_dead_until_host_evidence` pins the dead default leg.
  No new benchmark, waiver, score or publisher evidence is invented.
- Round 3: positions 7 (Ember) and 8 (OmniRoute DeepSeek) are review-only;
  absent catalog effort stays unset. Ordered legs retain registered effort and
  identity, never suffix-derived defaults. The fixed-order constants intentionally
  stay in catalog seed data: they reference the existing leg registry, not a second
  registry. Strict capability validation accepts omission only for permanently
  disabled, role-less declarations; explicit empty/null/invalid facts still fail.
  Hermetic probe tests guard module-level as well as function-level markers.
- Validation: failing regressions preceded fixes; all test names retained. Bounded
  affected run: 1425 passed, one unrelated wall-clock assertion failed at
  `tests/test_review_jev_hook.py:220` (2.09s versus 2s under load). All nine touched
  test files plus that entire unchanged file reran: `277 passed in 244.34s (0:04:04)`.
  No whole-suite run or benchmark-debt waiver; the final fix is under 350 changed lines.

| Position | Implement | Standard review | High review | Complex review |
| --- | --- | --- | --- | --- |
| 1 | Sol xhigh | Mimo Pro | Sol medium | Sol xhigh |
| 2 | Hydrogen high | OpenCode Go DeepSeek | Terra medium | Terra xhigh |
| 3 | Sonnet high, non-Claude-Code only | Mimo Flash | Hydrogen medium | Hydrogen high |
| 4 | Mimo Pro | Haiku | Mimo Pro | Mimo Pro |
| 5 | OpenCode Go DeepSeek | Ember Max | OpenCode Go DeepSeek | OpenCode Go DeepSeek |
| 6 | Mimo Flash | OmniRoute DeepSeek (dead) | Mimo Flash | Mimo Flash |
| 7 | — | — | Haiku | Haiku |
| 8 | — | — | Ember Max | Ember Max |
| 9 | — | — | OmniRoute DeepSeek (dead) | OmniRoute DeepSeek (dead) |

Planning: Fable 5.2 high. Terra is Sol's same-family fallback. Review excludes
writer families; Haiku needs diff <100k; Ember needs opposing eligible cheaper
verdicts. Missing effort is not a claim of medium effort. Unknown capabilities
remain disabled; D-5712's fixed-order Sonnet admission does not lift live D-3828.
