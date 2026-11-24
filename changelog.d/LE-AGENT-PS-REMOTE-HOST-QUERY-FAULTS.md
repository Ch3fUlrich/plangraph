`plangraph_agent_ps` takes each host's checkout path from the host registry
(`hosts.json`, by id, alias or SSH target) instead of a stale `ps-hosts.json`
`src`, so a healthy host no longer reports `host src missing`. A WSL or Windows
host gets a longer query budget, and one hung host can no longer hold the whole
listing: past the longest budget it is listed as `unknown` with its reason while
the other hosts' runs still print.
The generated ps host entry now carries the registry's `state_dir`, and the remote
query (ps, lifecycle and coverage forms, POSIX, WSL and Windows) runs with
`PLANGRAPH_SPAWN_STATE` set to it, so runs launched into a nondefault state directory
are no longer omitted.
