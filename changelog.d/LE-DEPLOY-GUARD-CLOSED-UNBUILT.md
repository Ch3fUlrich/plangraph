Add `deploy/manifest.toml`, the one table of every systemd unit and workspace binary with its host
roles, env file names and first-run check, and a CI test that fails for a unit file or binary target
the manifest does not name (an explicit `not_deployed` reason is the only way to leave one out).
`host-sync --install-release` builds and installs the binaries the manifest assigns to the host's roles
(it was a list of four), and `host-sync --install-units` installs and enables the manifest's unit families
for those roles, reporting a family whose env file is missing instead of starting it.
`plangraph deploy-status` compares the manifest with live systemd state and the installed release stamp on
the host (not loaded, not enabled, not active, last run failed, stale binary), lists every entry with its
age, writes the ledger the GO check reads, and `merge-close --deploy-status` prints the same lines.
`land-train go-verify` refuses a GO while a manifest entry is over 24 hours not live on a reporting host
(`deploy_not_live`), when a host ledger is stale or none exists, unless a dated routing exception in
`deploy/exceptions.toml` covers it; `pg-deploy-status.timer` keeps the ledgers fresh.
