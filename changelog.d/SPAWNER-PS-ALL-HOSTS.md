- Spawner ps optionally queries site-configured hosts concurrently with bounded SSH timeouts, retaining live record host identity and reporting unavailable hosts as unknown in CLI and MCP views.
- Keep the prompt-rules pointer and probe-cache freshness clarification in the main spawner guide after splitting operations documentation.

- Restore fixed public ps failure reasons and keep arbitrary stderr/exception text private. Report the underlying exception class and SSH exit code separately in bounded `error` metadata, discard oversized stderr without affecting successful JSON, and remove the SSH retry to preserve the no-retry invariant.
