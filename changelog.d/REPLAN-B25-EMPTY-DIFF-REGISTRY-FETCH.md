The native empty-diff check of a registry-rooted review `diff_ref` first resolves the base and
head commits through the registry (`ensure_commit`, which fetches the registry origin on a miss),
as the inline diff reader does, so an empty range in a checkout that has not fetched its commits
is refused as `empty_diff` before routing instead of looking unreadable and passing through.
`rust/pg-spawnd/tests/empty_diff_spawn.rs` drives the real MCP server with the registry loaded from `hosts.json` and asserts, for an
unfetched registry range and a path range, one `empty_diff` refusal per card, no route or launch adapter run and one record each.
