Capture and publish configured Keeper snapshots outside the global request lock, preserve
pending generations across retries, and keep message polling and merge refresh work off the
lock. Add lock-holder timing and new regression nodes for the configured paths, including a
required PostgreSQL held-out capture test.
Keep the timing logs on the daemon and preserve the request's 503 response when the
writer connection is lost before a snapshot can be built.
