### Routing inputs (D-5092, D-5113, D-5114)

- Retain real catalog tiers through route output; replace pin-era expectations
  with concrete cost-tier orders, without adding selector pins.
- Merge current main while preserving disabled Zen legs, explicit effort seats,
  Sonnet orchestration and the GPT-OSS review leg. Resolve catalog tiers through
  provider-qualified upstream identities and preserve measured within-tier ranking.
- Cover catalog tiers through planner candidates, selected legs, review seats
  and CLI JSON without changing benchmark rows.
- Register OmniRoute DeepSeek Flash and Muse Spark and Antigravity Sonnet/Opus
  review legs; Claude-family writers cannot receive Claude-family review seats.
- Host probes via the existing spawner probe mechanism: Sonnet passed (9.41s);
  Opus, DeepSeek Flash and Muse Spark returned exit 1 (3.36s, 2.73s, 0.13s).
  Results are recorded by the existing leg-state mechanism, not forced rankings.
