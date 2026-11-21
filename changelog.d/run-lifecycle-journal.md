Spawner writers and the review supervisor publish lifecycle changes through a
durable per-host journal before replacing the current run record. The spawner's
`runs --changed-since CURSOR_JSON` read retains terminal results and retries
unreachable hosts from their last successful cursor.

Legacy spawners keep writing private records until their Rust publisher is configured.
