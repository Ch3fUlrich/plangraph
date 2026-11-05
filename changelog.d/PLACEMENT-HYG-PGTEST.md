### Fixed
- Opted-in hosts reject bare test runs through bash_guard and direct callers to
  pgtest, which uses agent-tests.slice on Linux or a kill-on-close job on Windows.
- Handle Python option operands, Cargo toolchains, and wrapper option values
  without missed tests or false refusals; treat an unresolvable home as no marker.
