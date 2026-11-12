# Context decisions index (slice 1)

`rust/pg-ctx/src/decidx.rs` exports `parse(log, rules, operator_marker)` and
`write_tsv(rows, writer)`. No CLI subcommand is wired yet. Run the hermetic
inline-fixture tests with `cd rust && cargo test -p pg-ctx`.

Rows use the Markdown columns `id | project | decision | by | why / evidence`.
Non-D-id lines are ignored; duplicate ids fail. Row text excludes the id and
outer pipes: collapse whitespace within each cell, join cells with ` | `,
preserve Markdown and escaped pipes, then SHA-256 the UTF-8 bytes (lowercase hex).
Malformed D-id rows remain visible as `unknown`, rather than silently disappearing.

The caller supplies the resume-digest operator-author marker; its normalized,
case-insensitive presence makes a row `operator` (empty disables this check).
Otherwise router/routing-* in the author cell gives `router`, else `mechanical`.
Merely mentioning the operator does not establish operator authorship.

Explicit withdraw/supersede/lift edges give the replaced row `superseded_by D-id`;
both active and passive wording work. Conflicting edges or unresolved withdrawal
wording give `unknown`. Otherwise exact D-id references in supplied `(rule name,
rule text)` pairs give `in_force rule` (multiple rule names sorted and comma-joined),
else `history`. Supersession takes precedence over rule references.

TSV columns are always `d_id`, `kind`, `status`, `text_hash`, with one LF-terminated
row per decision sorted by numeric D-id; input order does not affect output.
Commit date/location, private index generation, CI freshness checks and routing
review of unknown/history-marked operator rows belong to later slices.
