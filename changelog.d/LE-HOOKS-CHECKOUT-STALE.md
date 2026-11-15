## Fixed

- Discover and sync checkouts backing effective Git hook paths and symlinked hooks,
  including external plangraph-hooks checkouts. Clean default branches fast-forward;
  dirty checkouts retain their changes and report an alert. List running service
  WorkingDirectory checkouts while retaining live-unit protection.
- Pre-push identifies its own checkout HEAD and refuses when locally fetched
  origin/main has missing commits older than a day, with a sync repair command.
