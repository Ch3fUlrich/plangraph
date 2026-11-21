Add a conditional benchmark refresh to `tools/bench_gate.py --refresh --db PATH`. The Rust command imports Epoch AI coding benchmark exports and OpenRouter model metadata, persists HTTP validators and attribution, and records stale reasons and price disagreements in the catalog view.
Seed reference rates for the OpenRouter MiMo cells. Repricing uses the measured per-task input and output counts when those counts are available.
Select Rust checks when the shared SQLite schema or seed changes.
The loopback first-run fixture verifies 300 distinct effort cells, including all five Fable efforts, before checking a zero-write HTTP 304 refresh.
