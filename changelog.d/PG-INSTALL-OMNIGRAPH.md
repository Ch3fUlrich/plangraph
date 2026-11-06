# PG-INSTALL-OMNIGRAPH

Default host bootstrap now caches the MCP client from the repository pin,
connects future sessions using private host configuration, and performs a
secret-silent, fail-soft health check. Both launchers share the same helper.
