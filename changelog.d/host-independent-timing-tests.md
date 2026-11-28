Host-dependent and load-sensitive tests no longer read real host state or rely on tight
wall-time bounds. pg-repo-sync tests run the binary with the fixture as HOME and an empty
XDG config home (and refresh.rs's Git helper uses the shared isolated fixture command), so a
host's global Git config, such as a core.hooksPath, cannot add output. pg-spawnd
transport_unix tests bind under a short /tmp directory, not TMPDIR (a long TMPDIR overflowed
sun_path). The adapter and launch timeout tests repeat with a longer timeout when a loaded host
killed the script before it wrote its pid, and bound the elapsed time by timeout + 10 s instead
of 2 s. agent_view, inline-diff, launch descendant-pipe, quota poll and remote_ps deadline tests
widen the gap between the bound and the slow path so the bound can be generous.
