# pg-ctx

Shared context-tool crate; slice 1 exposes `instr_lint::lint(root, allowlist)`.
Run `cd rust && cargo test -p pg-ctx`. CLI/subcommand wiring comes later.

The linter recursively discovers CLAUDE.md, AGENTS.md and SKILL.md (excluding
Git metadata and symlink directories), then follows linked local text files once.
Inline Markdown destinations resolve beside their source; backtick paths also
try the repository root, matching repo skills. Fragments/queries are stripped;
URLs, absolute paths, commands and code globs are ignored. Targets outside the
root fail as dead links; binary linked files are not scanned as instructions.
History markers live in `HISTORY_MARKERS`; each matching line counts once.

The optional allowlist is a UTF-8 file under the root: `path|check|count`, using
repo-relative slash paths and `history-note` or `dead-link`. Blank/comment lines
are ignored; malformed/duplicate entries error. Counts suppress that many hits;
additional hits fail, and excess budgets produce `stale-allowlist` findings at
the allowlist line. Remove or lower stale entries: the linter never rewrites them.
Returned failures sort by path, 1-based line, check and message, without root paths.
