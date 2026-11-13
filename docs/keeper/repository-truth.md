# Repository truth

`pg ready` checks every dependency-ready candidate before limiting the output.
Merged work and outdated premises appear under `pg ready --needs-reconcile`;
`pg ready --in-progress` names the open PR numbers whose commits carry the key.
These partitions use the same checks as claim. Age alone remains advisory.

Build/install `pg-keeper-truth` from `rust/pg-ctx`. For remote-tracking default
refs (normally `origin/main`) the keeper invokes its `index REPOSITORY REF`
command and caches the result per repository/ref for 60 seconds. Concurrent
refreshes share the adapter lock. Override its
executable with `PG_KEEPER_TRUTH_BIN`. The command refreshes origin, indexes the
default history once, lists open PR heads once, and indexes each head's delta.
It compares parsed Plan-Item trailers exactly, including repeated and bare keys.
No per-item Git invocation is needed. Probe failure or truncated PR discovery
puts candidates into needs-reconcile as unknown; claim also refuses unknown.
Bare default refs resolve to their refreshed origin counterpart. Offline fixtures
may explicitly set `PG_KEEPER_TRUTH_LOCAL=1` to retain the local-only adapter;
this mode cannot establish live claimability. Shallow history is unknown.

JSON ready rows include `repository_status`, `prs`, and `reconcile_reason`.
The reconcile/progress filters require the keeper; an offline snapshot cannot
provide these repository facts. A cached ready answer can change on its next
refresh, so claim remains authoritative at the time of the request.

Deployment and live counts belong to the orchestrator. This worktree does not
write to the live keeper, install binaries, or change timers.
