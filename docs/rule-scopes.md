# Rule scopes (slice 1)

`pg_ctx::scopes` parses the future `router/rule-scopes.tsv` and validates numbered
`DECISIONS.md` lines from supplied UTF-8 text, without network or keeper access.
Run `cd rust && cargo test -p pg-ctx`.

The TSV header precedes all rows. Required directives are `# roles<TAB>all,...`
and `# tags<TAB>all,...`; tag vocabulary has at most 20 distinct entries including
`all`. Optional mappings are `# project<TAB>name<TAB>tag,...` and
`# kind<TAB>name<TAB>tag,...`. Names are case-sensitive; comma-separated values
are trimmed. Duplicate vocabulary directives/mapping keys, unknown directives,
empty fields and malformed rows fail. Blank lines are ignored.

Rows contain exactly `rule_no<TAB>role,...<TAB>tag,...`. Rule numbers preserve
letter suffixes and leading zeroes (`0.2a`, `2.1A`, `4.00`). Validation compares
them with leading numbered DECISIONS lines, ignoring headings and inline cites.
Unknown roles/tags, over-20 tag sets and duplicate rule rows fail at parse time;
`validate` returns named `MissingRow`, `RemovedRule` or `NoRules` errors.
Line-bearing errors are one-based; `InvalidHeader(0)` means a required vocabulary
or its `all` entry is absent. Validation returns the first deterministic error.

`item_tags` unions project tags, kind tags and explicit scope labels (deduplicated).
Unknown projects/kinds contribute nothing; labels need not be vocabulary entries.
`applies` first checks the role (`all` in a row matches any role), then intersects
tags (`all` in a row matches any item). Empty item tags select every rule of that
role, never rules for another role. Empty row tag/role sets are invalid.

Hermetic tests copy synthetic fixtures into temporary directories. The real data
TSV, routing's tag review, data CI and `pg-ctx rules` wiring are later slices.
