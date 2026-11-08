## Critical-path stall alerts

`python3 tools/crit_path_stall.py --config CONFIG` reads the keeper snapshot
and last activity of open transitive dependencies of `plangraph:RT-P1-E2E`
(`--target` overrides it). Only `claimed` and `in_review` items alert: idle time
must strictly exceed 12h or 24h (override with `--thresholds FIRST SECOND`,
positive increasing durations). Alerts go to the owner (falling back to
`claimed_by`) and `routing` (`--routing` overrides it) through the signed fleet bus;
the check never mutates keeper state.

`crit-path-stall.json` beside the configured keeper database deduplicates each
threshold/recipient within an episode keyed by target, state, last activity and
epoch. New activity or state resets the episode; successful deliveries are
saved individually with atomic replacement, and a local flock serializes runs.
A crash between delivery and saving can replay a message. The optional
`plangraph-crit-path-stall.timer` runs its matching service every 30 minutes
(`Persistent=true`). `--dry-run` prints pending alerts without sending messages
or creating/updating state, locks or directories.
