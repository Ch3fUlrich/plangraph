# Spawner reliability

## RL26: exact leg references (P-R6)

The Rust resolver accepts exact registered leg ids first, or unique exact model
references eligible for the requested role. Ambiguous models refuse; bounded,
deterministic distance hints never choose a route. No provider family inference
or route-group data is used.

This is inactive groundwork, not an enabled reference-admission slice.
The existing Python route verb cannot accept a leg selector. Catalog model ids
are not necessarily registered leg ids. Activating admission requires resolving
both contracts without bypassing planner availability policy. No live canary or
historical gate has been satisfied; rollback remains off.

Switch parsing supports environment-over-file precedence and off by default;
warn/shadow means observation only. The pure loader rereads the injected config
path, so a subsequent call sees updates immediately. Runtime wiring, cached
catalog admission, and bounded warning deduplication remain unimplemented.
