## FLEET-AGENT-VIEW (D-4242, D-4245)

Add a read-only local and SSH fleet agent inventory and an Agents tab. Private
snapshots are atomically written with mode 0600; only seven agent fields reach
the rendered page. Unreadable hosts remain unknown, and old snapshots are marked
stale. Existing host metrics and their running-agent count rule are unchanged.
