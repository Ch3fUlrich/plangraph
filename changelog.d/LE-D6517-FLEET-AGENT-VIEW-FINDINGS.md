Fixed the D-6517 findings of the fleet agent view: the collector redacts secret-shaped strings
before it applies the private-word list, `pg-spawnd agents-view --publish` (run by the renderer)
projects the stored inventory again with the secret redactor and the current private-word list
(an unusable list hides every host, the timestamp and the unavailable reasons are allowlisted),
and the scheduled `--local` run lists configured remote hosts as unknown (`not_collected`)
instead of dropping them.
The agent-view tests build `pg-spawnd` in the tree's own `rust/target` and use the path Cargo reports.
