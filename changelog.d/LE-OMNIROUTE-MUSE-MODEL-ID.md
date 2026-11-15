Correct the OmniRoute Muse leg to `omniroute/spark-1.3-contributor` and declare
that identity in the shipped provider config (D-6054). Add Rust CI coverage of
enabled OpenCode default, explicit and review fallback legs using the existing
overlay, renderer and host admission helper. Named exclusions retain external
catalog dependencies and the existing Grok declaration gap; unknown model
failures identify the leg. No live availability claim is made.
Register renderer inputs with the CI selector. Retire the obsolete benchmark
pending identity while keeping the corrected Muse identity explicitly unmeasured
and the frozen benchmark debt ceiling unchanged.
