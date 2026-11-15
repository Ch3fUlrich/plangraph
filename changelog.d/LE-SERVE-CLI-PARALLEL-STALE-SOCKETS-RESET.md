Stale Unix socket test fixtures now bind without listening, preventing concurrent
child spawns from inheriting a temporary listener that can accept and reset an
MCP client before exec. A regression keeps a duplicate descriptor open to verify
that the stale fixture still refuses connections.
