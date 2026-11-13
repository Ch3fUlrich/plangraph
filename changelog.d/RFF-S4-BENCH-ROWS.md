Resolve exact provider-qualified benchmark rows for planner legs without changing utility selection. Missing measurements remain eligible with an explicit benchmark gap; no measurements are fabricated.
Consolidated S2-S5 follow-up: retain exact benchmark aliasing and missing-row gaps, preserve measured cross-family utility order, and keep the generic resolver's epsilon-band ranking rather than post-sorting it by tier or model name.

Antigravity Opus benchmarks use exact provider-qualified keys. Review routing preserves input-table order within each measured family: MiMo Flash may precede MiMo Pro despite different utilities; that policy is not evidence of better measured utility. No intelligence, throughput, or cost measurements are fabricated.

Historical S4 probes recorded Sonnet passing (9.45s), Opus passing (9.41s), DeepSeek Flash failing (exit 1, 4.43s), and Muse Spark failing (exit 1, 0.14s) through the existing leg-state mechanism. Probe latency is not a throughput or intelligence benchmark.
