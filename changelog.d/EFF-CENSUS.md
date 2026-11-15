EFF-CENSUS: `python tools/claude_usage.py --census --since 7d --json` now
runs a Rust census of local transcripts with paginated `gh` merge accounting.
It reports role costs, trigger shares, costs per merged PR and the three-day
58-merges/day guard. Existing usage commands retain their behavior.
Synthetic pytest and Rust fixtures cover the census; live evidence is attached
by the orchestrator after merge. See docs/claude-census.md for classification limits.
