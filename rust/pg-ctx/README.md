# pg-ctx navigation

Build and install the CLI from the Rust workspace with
`cargo install --locked --path pg-ctx`. Run `pg-ctx nav-build` from any
directory in the worktree. The command reads committed Git blobs and
atomically writes the index at `git rev-parse --git-path pg-ctx-nav.json`.
Schedule the same build command in the consumer's main-checkout update
job to produce a fresh index after merges.

Generate SCIP inputs with `rust-analyzer scip` and `scip-python index` from
the same clean commit. Pass each output as `--scip FILE`. Use
`--scip-at rust FILE`, for example, if the producer's project root was the
repository's `rust/` directory. The producer must match the commit being
indexed. SCIP files can omit embedded source and don't carry a Git commit.
The build validates paths, source ranges, and embedded text if provided.

Pass `--rules FILE` for an external snapshot of `router/DECISIONS.md` and
`--links FILE` for keeper item references. The links file is a JSON array
with one object per item, for example:

```json
[{"key":"example:NAVIGATION","title":"Navigation","refs":["src/lib.rs::navigate","D-10"]}]
```

`pg-ctx nav 'src/lib.rs::navigate'` returns JSON for a symbol, qualified
symbol, file path, rule identifier, or keeper key. Definition locations
and document sections use one-based inclusive line ranges. SCIP columns
are zero-based UTF-8 byte offsets. Call sites combine SCIP target identity
with tree-sitter call syntax. The report includes SCIP coverage for
relevant paths so an empty caller list can be interpreted in context.
The same query is available as `pg_ctx::nav::navigate`.

When HEAD changes or tracked files are edited, the query reports
`stale: true` and lists changed paths alongside the indexed result.
Rebuild after committing changes to get locations for the new snapshot.
Untracked files are outside the index.
