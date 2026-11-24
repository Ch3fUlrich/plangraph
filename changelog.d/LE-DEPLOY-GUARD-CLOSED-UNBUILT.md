Add `deploy/manifest.toml`, the one table of every systemd unit and workspace binary with its host
roles, env file names and first-run check, and a CI test that fails for a unit file or binary target
the manifest does not name (an explicit `not_deployed` reason is the only way to leave one out).
