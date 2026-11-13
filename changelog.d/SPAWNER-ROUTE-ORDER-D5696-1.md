- D-5696/D-5706 slice 1: add fixed implement, standard/high/complex review and
  planning catalog inputs to the existing `payment_order` core. Effort-bearing
  registrations, writer-family exclusions, legacy replacement refusals, the
  strict Haiku diff limit and cheaper-verdict Ember escalation are hermetic.
- Take PR #818's DeepSeek served-id correction into the new catalog and relevant offline/launch
  regression coverage; do not infer host availability from a corrected id.
- This slice does **not** activate the tables in `plan_spawn`, change live
  implement admission, replace utility resolver selection, or fence legacy
  explicit targets globally. Those require migration of the existing route
  policy/role and historical test contracts beyond this slice's ~400-line budget.
  Live probes, run-tree updates and host catalog deployment remain L1-owned.
- Leave PR #818's global default-leg mutation out: it introduces a new missing-row
  benchmark violation against the frozen admission ceiling at
  `tests/test_benchmark_data_contract.py:48`; no waiver or benchmark is fabricated.
