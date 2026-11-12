Host admission now shares a RAM-only rule between CLI and overflow, reserving
the registry role ramp above the host memory floor. Overflow ranks by RAM
headroom; generated configuration and readiness no longer enforce run counts.
Missing registry data excludes remote hosts and logs conservative local RAM
defaults. The existing OOM guard is unchanged.

Fold HOST-OVERFLOW-DEFAULT (#693): default CLI/MCP spawns whose local RAM
admission refuses try healthy SSH hosts only for `public_repos`-allowlisted
repositories. Private/unknown repositories queue without copying; exhausted
targets queue with safe per-host reasons. Missing overflow configuration still
enforces the RAM floor on CLI, MCP and seat spawns; below-floor runs queue
without launching locally. Admission lives in the existing seat spawn module.
Dry runs stay local and local
benchmark databases are not forwarded. Missing/unreadable remote receipt IDs
are saved as acknowledged_unknown/status=unknown on the selected host, never
retried. Legacy local limit overrides do not affect RAM admission.
