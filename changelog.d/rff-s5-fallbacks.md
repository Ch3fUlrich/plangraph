### ROUTE-FREE-FIRST S5 (D-5092 / D-5113 / D-5114)

- Replace fourteen pin-era route test expectations with concrete utility outcomes;
  isolate the benchmark database so host measurements cannot perturb assertions.
- Register personal/free Argon, OmniRoute DeepSeek Flash and Muse, and Antigravity
  Opus; expose Antigravity Sonnet to review while preserving Claude-family exclusion.
  Existing paid `deepseek` already targets `omniroute/deepseek-direct-flash`.
- Utility audit: no scoring or sorting changes; roles, model identities, tiers,
  privacy and availability remain inputs. Personal Argon has a distinct provider
  key from Go Argon, so Go quota state cannot poison the personal credential.
- Host probes through the existing spawner probe recorder: Sonnet passed (7.36s),
  Opus passed (9.36s after correcting its advertised model identity); OmniRoute
  DeepSeek and Muse failed (exit 1); personal Argon and Go DeepSeek failed (exit 7).
  Go Argon was already limited and correctly skipped without another request.
  These are availability observations, not invented performance measurements.
- Host route output (20k, review excludes Claude): implement = openrouter-free,
  big-pickle, Sol, AGY Gemini Flash, AGY Argon, MiMo Flash, MiMo Pro;
  review = personal Argon, personal DeepSeek free, big-pickle, AGY Gemini Flash,
  AGY Argon, Go DeepSeek. Inline-only reviews need diff metadata to join this list.
- Open: diagnose failed upstream probes; prove successful personal-credential
  service after Go 429, observe next ten production spawns, merge and CI.
