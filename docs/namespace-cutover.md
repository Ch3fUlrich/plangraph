# Host namespace cutover

`setup/manifest.toml` inventories legacy/new paths, consumers and classifications.
`plangraph namespace inventory` prints that static inventory and the paired
operator task/gate edge. It never scans or opens secret contents.
`namespace check` uses names, links and permission metadata only. `namespace apply` (also `namespace apply --dry-run`) previews moves without
creating roots; `namespace apply --yes`
only renames explicitly classified non-secret/state paths and leaves old paths
as symlinks. It does not execute automatically during setup.

PLANGRAPH_DATA_ROOT selects the operator-owned private data checkout/directory
for non-secret configuration. Set an absolute path explicitly; no deployment
path is assumed. Private configuration, environment files, shell profiles, environment.d
and container storage remain operator-only. Secrets require 0600 files and 0700
directories on Unix, and private user ACLs on Windows. Windows ACL privacy stays operator-only and is never certified by mode bits.
Windows symlink creation requires the normal OS developer-mode/privilege support.
A failed move rolls back completed moves and removes their generated shims.
Rollback never overwrites a recreated old path: a concurrent conflict retains both
copies and exits 3 for manual reconciliation. Private lock/control files may remain.
Operator-only inventory rows are informational during apply; check still reports
them as drift until the operator completes that work. No secret contents or values
are printed or logged.

Run `migrate-breaker` and `cost-gate migrate` before `namespace apply --yes`.
Namespace check/preview/apply refuse when existing legacy state lacks its import
completion marker. Imports and namespace writes share one migration lock.
When breaker state was copied already, namespace apply archives the old directory
under the new state root at `legacy-namespace/breaker`, then creates its old-path
shim. Active state always wins; the legacy copy remains available for rollback.
Already relocated roots make repeated imports no-ops. Existing archive paths are
never overwritten. Unknown
remaining state/share entries require operator classification; the root namespaces
are never recursively moved by the program. A crash after rename can be resumed:
the destination remains intact and the next apply creates the missing shim.

Consumer cutover inventory:

| Consumer | Target and required operator action |
| --- | --- |
| Both gateway containers | Move `ai-stack/{omniroute,qoder-home,opencode-home}` to `.local/share/plangraph/ai-stack`; retain the old bind-path alias as a symlink. Stop containers first, restart after mount verification. |
| Shell profile | Source `.config/plangraph/omnigraph.env` from the existing profile; preserve unrelated profile text. |
| environment.d | Link `60-plangraph-omnigraph.conf` to the new env file and retain the old filename shim until the removal guard clears. |
| fleet-poll | Use `.local/state/plangraph/fleet-poll` and private `.config/plangraph` files. |
| ctx-watchdog | Use `.local/state/plangraph/ctx-watchdog` and the Rust breaker path. |
| Hooks and units | Repoint env/config references to `.config/plangraph`; state references to `.local/state/plangraph`; apply the router decision for unit changes. |
| daily cost gate | Operator converts old limit names privately to the new daily-gate TOML; the value is never copied to public data. Import state with the cost-gate command. |

The manifest pairs `plangraph:HOST-NAMESPACE-OPERATOR`, requiring this code item,
with a gate edge to `autoos:REMOVE-AUTOOS`. This is a reviewable task declaration;
the operator must add it to the private keeper plan before live work. No keeper
write or invented router decision was made by this package.

Rollback: stop affected consumers; remove only known shim links; rename each
new path back to its recorded old path; restore old profile/environment.d/unit
references and container bind paths; restart and verify. Keep backups, aliases
and source state until the removal guard is clean. Do not delete host state as
part of code review. A root-level config shim is created by the operator only
after all secrets have moved and per-file non-secret shims remain reachable.
