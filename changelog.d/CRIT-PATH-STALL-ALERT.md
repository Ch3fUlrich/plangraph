Add read-only critical-path stall notifications for transitive open dependencies
of plangraph:RT-P1-E2E. Claimed/review items alert their owner and routing after
12 and 24 hours without keeper activity, once per threshold/activity episode.
Use `python3 -m plangraph.keeper.crit_path_stall --config keeper.json --dry-run`
to preview; `--target`, `--thresholds 12h 24h`, and `--routing` are configurable.
Dedupe state is `crit-path-stall.json` beside the configured keeper database.
Delivery uses the existing signed fleet-bus client (including its durable spool).
The optional user service/timer is not installed automatically; customize its
checkout path and provision its EnvironmentFile with the existing keeper read
and fleet-bus client configuration before enabling. Checks run every 30 minutes.
