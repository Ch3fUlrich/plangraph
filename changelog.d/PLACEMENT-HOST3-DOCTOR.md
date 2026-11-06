### Fixed

- The host doctor now silently reads `~/.config/plangraph/watchdog.env` before
  checking readiness, matching watchdog service defaults in a clean shell while
  preserving existing environment overrides. Missing or unreadable files are
  ignored; loaded values are not reported.
