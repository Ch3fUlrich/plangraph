Spawner: quota refusals fail fast and routing reads omniroute `quota_snapshots` (SPAWNER-QUOTA-SNAPSHOT-FAILFAST). A leg whose provider pool is at 0% (every account) is skipped until its reset and routes again after it; a missing or unreadable snapshot never blocks routing. The omniroute "quota policy filtered" / "reached configured quota threshold" lines and agy "RESOURCE_EXHAUSTED (code 429)" are detected like opencode-go 429s (error-shaped lines only); a silent gateway leg is checked against the call log (sessionTag) 60-90 s after it stalls, and a logged quota refusal ends the run, books the leg limited with its reset, and falls through to the next leg.

A configured read-only `QUOTA_SNAPSHOT_DB` preserves `next_reset_at`; API rows without a reset or timestamp hold for 30 minutes from observation.

Local push checks select affected files against origin/main and refuse full-suite execution (operator package R2).

The host-view integration test declares its Rust projection-helper dependency through the existing shared helper; CI still requires that binary.
