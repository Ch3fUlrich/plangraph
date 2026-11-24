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
An item with a `Live:` clause can no longer close without a verified deploy evidence row: automatic close and
router GO close hold it as `deploy_pending: live_evidence_missing`, and a passing review or a close on decision
is refused with `evidence_unverified: live_evidence_missing`.
`plangraph resume-digest` ends with a `deploy-not-live N oldest ...` line from the deploy-status ledgers and
`plangraph deploy-status --summary` prints it alone for the operator's daily line.
Every item, not only one with a `Live:` clause, now needs a verified deploy evidence row or a
`no deploy needed: <reason>` line to close (D-6790); an empty reason is refused as `no_deploy_reason_empty`.
`host-sync --install` is the one command that makes a host complete from the manifest (plan, `--check`, install
binaries, units, timers and env defaults, ledger, idempotent), `plangraph-host-sync.timer` runs it, and
`host-sync --install --fleet` runs it on every host of the hosts registry over ssh from the center.
The landing step now asks `plangraph land-train deploy-guard --refresh yes` before it merges or fast-forwards: every
`local` and `ssh` host of the hosts registry must have a fresh ledger and no entry over 24 hours (in seconds) not live.
A timer is live only when its service is loaded and has run to success; a pending daemon reload keeps a unit not live and
the installer reloads on retry; the release stamp includes embedded files outside `rust/` (an exceptions-only change is a
new release); unit files under `deploy/liveness`, `deploy/pganchor` and `deploy/watchdog` are manifest entries; and the
daily `landing report --day` carries the `deploy-not-live` line.
