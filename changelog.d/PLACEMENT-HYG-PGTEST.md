### Fixed
- Keep the six placement wrappers local, read the marker once per top-level
  check, and consolidate placement documentation into one short section.
- Keep systemd-run boolean flags value-free so background and environment-expanding
  invocations cannot hide bare tests, including through nested wrappers.
- Opted-in hosts reject bare test runs through bash_guard and direct callers to
  pgtest, which uses agent-tests.slice on Linux or a kill-on-close job on Windows.
- Handle Python option operands, Cargo toolchains, and wrapper option values
  without missed tests or false refusals; treat an unresolvable home as no marker.
- Keep placement usage in docs/TESTING.md rather than the merged-changes log;
  verify the guard rules remain within the 400-line limit.
