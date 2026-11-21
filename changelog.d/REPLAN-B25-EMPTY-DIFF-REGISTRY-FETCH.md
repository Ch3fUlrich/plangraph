The native empty-diff check of a registry-rooted review `diff_ref` first resolves the base and
head commits through the registry (`ensure_commit`, which fetches the registry origin on a miss),
as the inline diff reader does, so an empty range in a checkout that has not fetched its commits
is refused as `empty_diff` before routing instead of looking unreadable and passing through.
A spawn-level test drives `spawn()` and asserts one refusal, no routing and exactly one `empty_diff` record with empty legs.
