Replan groups: output boundaries, record ownership/state parity, integration
contracts. Restore shaped stderr evidence, exclude stopped legs in both services,
publish witnessed legacy records without job_id, and allow queued needs_input.
CR flushing already consumes only scrubbed OutputLines segments; collector tests
now cover cross-read tokens. Adapter models has one callable-aware consumer.
CLI/MCP catch SpawnRefused with exit_code 2; exhaustion-specific transport tests
remain a coverage gap. Default hung activation requires host-owned sampling and
leg-scoped cancellation and remains unresolved rather than guessing ownership.
Validation: 281 Python tests, two Rust tests, size gate pass; one subprocess
cleanup warning. Existing oversized runner/service files remain above 400 lines;
no allow-list/baseline changes. Publication, service selection and prose mutations
are rejected; mirrored runner and Rust changes were not mutation-checked.
