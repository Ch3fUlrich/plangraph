- Add `og-memory check-mcp-url` so fleet diagnostics can flag the retired
  Omnigraph loopback endpoint in a daemon or session environment. The check
  uses the environment variable name already referenced by the MCP entry and
  exits with a fixed diagnostic for the retired URL.
