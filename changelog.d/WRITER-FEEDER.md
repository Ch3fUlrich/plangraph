Added a Rust `plangraph feed --once [--dry-run]` writer feeder with deterministic
PR/CI/priority/age ordering, reconciliation and overlap exclusions, host admission,
keeper claims with failure release, deterministic briefs and JSONL decisions.
Includes two-minute systemd unit examples and configurable policy/house rules;
units are not installed automatically.
Host policy accepts full hostnames through short public aliases.
Feeder template/policy changes select the Rust CI tests.
Uses keeper note-fold metadata and legacy reconciliation context, tries the next
item after a claim refusal, and preserves claims for queued/uncertain spawner
receipts. Host aliases, terminal states and fresh quota variants follow the
existing keeper/spawner contracts, with offline Rust adapter regression tests.
Preserves remote/queued run identity and detached writers across timer exit;
isolates malformed candidates, releases unadmitted failures, reserves RAM against
post-sample launches, and atomically maintains/prunes receipt associations.
Pins launches to the admitted host so spawner overflow cannot bypass host caps.
Finished watchdog-stalled and terminated-after-answer records release feeder capacity and overlap scope.
Run records lacking item identity recover it from their launch card; unidentified
writers consume only their own host slot without refusing the whole tick (D-6050).
