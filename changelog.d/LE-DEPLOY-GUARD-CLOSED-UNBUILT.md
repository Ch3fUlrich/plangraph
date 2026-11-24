Add `deploy/manifest.toml`, the one table of every systemd unit and workspace binary with its host
roles, env file names and first-run check, and a CI test that fails for a unit file or binary target
the manifest does not name (an explicit `not_deployed` reason is the only way to leave one out).
`host-sync --install-release` builds and installs the binaries the manifest assigns to the host's roles
(it was a list of four), and `host-sync --install-units` installs and enables the manifest's unit families
for those roles, reporting a family whose env file is missing instead of starting it.
