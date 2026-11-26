- Added the Rust `plangraph flow-snapshot` command to collect read-only watchdog
  observations and publish the existing snapshot format atomically. Required read
  failures preserve the preceding snapshot. Optional gaps carry source-specific
  diagnostics, including unavailable operator-order and host-idle resource evidence.
