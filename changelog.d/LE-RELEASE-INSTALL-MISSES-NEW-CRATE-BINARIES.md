`host-sync --install-release` derives its binary list from the workspace: every crate marks its host binaries
in `[package.metadata.plangraph]` (`host-binaries`, or `optional-host-binaries` for the keeper backup) and the
installer reads them from `cargo metadata`. `pg-telemetry` is marked, so it reaches each host with the next
host-sync run, and a new crate ships by marking itself. A mark naming a binary the crate does not build, or a
marked binary missing from the plan, fails the install before the build. The log line is now
`RELEASE INSTALLED <path>`. The check reads the actual cargo build arguments and the published list, and `pg-telemetry --version` now prints its version. A `host-binaries` mark of the wrong type fails the install before the build.
