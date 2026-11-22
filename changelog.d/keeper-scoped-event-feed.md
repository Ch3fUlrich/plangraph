Keeper exposes paginated events for the authenticated principal, including claim and
release transitions between scans. Event recipients are recorded atomically with
each new event. Use `plangraph keeper events --since SEQ` to replay authorized rows.
