# COMMIT-MSG-HOOK-GIT-PARITY

Outcome. The pg-hook commit-msg check validates exactly the message Git saves for empty -m arguments, the core.commentChar=auto prefix Git chose before editing, --cleanup=scissors without an editor, and --no-message.

- plan_source. Keeper item plangraph:COMMIT-MSG-HOOK-GIT-PARITY, 2026-11-24 23:51Z, priority 10, D-6860 (slice S2 of the b172 replan after the rule 3.35 stop), D-6861.
- base commit. 6e0289ba17daa2ada6e862f35601ab6964e62cab (head of lane/batch172-b133-replan-r8; not on main at 443bf493a).
- card SHA-256. e443d03a7fca6275e9ac50d2f82fc469d883a4b07208019677c703d6206d9378
- tree SHA-256. 5d19989b9abb264dd6fa222cd4da61d751b60e21c3e7874d090674b8d1002a8c
- evidence_after_deploy. After the lane lands, run `pg-hook` install (`commit-msg --install <checkout>`) in one main checkout and make one real commit with `-m "" --no-verbose -m <message>`; record the hook verdict and `pg-policy --class-search --range <base>` exit for that commit in the item evidence.

## Checked against base

- Main at 443bf493a has no `rust/pg-hook/src/commit_args.rs` and no `rust/pg-policy/src/lib.rs`; both, and the Class-Search check in `commit_msg.rs`, exist only on lane/batch172-b133-replan-r8 (commits bae733b7a..6e0289ba1). This item therefore builds on that lane and lands with plangraph:REPLAN-B133-HYDROGEN-FINDINGS. [rust/pg-hook/src/commit_msg.rs@6e0289ba:1-3,170-210]
- F1. `parent()` drops every empty argument of `/proc/<ppid>/cmdline` (`filter(|arg| !arg.is_empty())`), so `-m "" --no-verbose` parses as `-m --no-verbose`. [rust/pg-hook/src/commit_args.rs@6e0289ba:113-128]
- F2. `auto_comment_char` takes the last standalone candidate line of the edited file, so an editor that appends `#` overrides the `;` Git chose. [rust/pg-hook/src/commit_msg.rs@6e0289ba:87-97]
- F3. `strips_comments` returns true for `scissors` without an editor; Git turns scissors into whitespace cleanup when no editor runs and keeps comments. [rust/pg-hook/src/commit_msg.rs@6e0289ba:77-85]
- F4. `--no-message` leaves `message` true (`parsed.message |= !negated`), so the hook assumes no editor where Git opens one. [rust/pg-hook/src/commit_args.rs@6e0289ba:84-88]
- Existing helpers to reuse: `repo()`, `git()`, `commit_with_env()`, `saved_body()`, `BAD`, `MARKER` in `rust/pg-hook/tests/commit_message.rs` (598 lines; no room under the 800-line gate for four more tests).

## Decisions

- D1. Git's own rules are the reference (builtin/commit.c `opt_parse_m`, `adjust_comment_line_char`, cleanup selection). The hook reproduces them; it does not rewrite the user's file.
- D2. Argument reading. `parent()` splits cmdline at NUL, removes only the one empty piece after the final NUL, and keeps every other empty argument. `CommitArgs` replaces `message: bool` with `m_text: Option<String>` (the joined `-m` buffer, `None` when no active `-m`) and two independent flags `file_source: bool` (`-F`, `--file`) and `reuse_source: bool` (`-C`, `--reuse-message`). `-m V` appends as Git does: if the buffer is non-empty add `\n`, add `V`, then end the buffer with `\n` unless it is empty. `--no-message` sets `m_text = None`. `--no-file` clears only `file_source`; `--no-reuse-message` clears only `reuse_source` (so `--no-file` never cancels an active `-C`, nor `--no-reuse-message` an active `-F`). `uses_editor()` is `edit.unwrap_or(!(m_text.is_some() || file_source || reuse_source))`.
- D3. Auto comment character. When `core.commentChar` (or `core.commentString`) is `auto` and `m_text` is `Some`, compute the character from `m_text` exactly as `adjust_comment_line_char`: `#` when the buffer holds no `#` at all; else the first of `#;@!$%^&|:` that is not the first byte of the buffer and not the byte after any `\n` or `\r`. This value replaces `auto_comment_char` for both the scissors prefix and the stripspace prefix. For every other source (`-F`, `-C`, `-c`, template, amend without `-m`, unknown parent), keep today's last-standalone-line rule; the docs name this limit.
- D4. Cleanup. `strips_comments(cleanup, editor)` becomes: `strip` -> true; `scissors` -> `editor`; `whitespace` | `verbatim` -> false; default -> `editor`. `git_cuts_at_scissors` is unchanged.
- D5. Parity proof in tests. A test helper `range_agrees(root)` reads the saved body (`git log -1 --format=%B`) and returns true when `message_violations` of the copied `tools/ci_commit_gate.py` is empty (same `runpy` bridge as the hook) and every `Class-Search:` line passes `pg_policy::parse`. Each case asserts: hook accepts <=> `range_agrees`. For a refused case the test repeats the same commit with `--no-verify` and asserts `range_agrees` is false.

## Entry points

- E1. `rust/pg-hook/src/commit_args.rs::parse` and `::parent` (argument model, D2).
- E2. `rust/pg-hook/src/commit_msg.rs::run` with `strips_comments` and the auto prefix selection (D3, D4).

## Acceptance

| Row | Entry | Observable result | Evidence |
| --- | --- | --- | --- |
| B1 | E1 | `commit.verbose=true`, `git commit --allow-empty -m "" --no-verbose -m "x\n\n<MARKER>\n<BAD>\n\nWriter: codex"`: the hook refuses with `malformed Class-Search`; the `--no-verify` repeat saves a body that `range_agrees` rejects. | X1 |
| B2 | E2 | `core.commentChar=auto`, `--edit -m "x\n\n# literal\n\nWriter: codex"`, editor `sh -c 'sed -i "/^;/d" "$1"; printf "#\n" >> "$1"' --` (removes Git's generated `;` comment block, then appends a standalone `#` line): Git chose `;` and saves `# literal` and the trailing `#` line, so the hook refuses with `missing Writer trailer` (the base accepts); with the editor appending nothing the hook accepts. Both verdicts equal `range_agrees` on the saved body. | X2 |
| B3 | E2 | `git commit --allow-empty --cleanup=scissors -m "x\n\nWriter: codex\n# retained"`: the hook refuses with `missing Writer trailer`; `--cleanup=scissors -m "x\n\n# kept\n\nWriter: codex"` is accepted and the saved body keeps `# kept`. | X3 |
| B4 | E1 E2 | `git commit --allow-empty -m first --no-message --verbose` with an editor that writes `x\n\nWriter: codex` above Git's template and `<BAD>` below Git's scissors line: the hook accepts and the saved body has no Class-Search line. | X4 |
| B5 | E1 | Unit level: empty arguments survive parsing, `-m` buffers join as Git joins them, `--no-message` clears the buffer. | X5 |

## Verification

Write X1-X5 first on 6e0289ba1 and observe each fail (X1, X2, X4 on hook verdict, X3 on the refused/accepted pair, X5 on the parsed fields).

- X1. `rust/pg-hook/tests/commit_message_parity.rs::an_empty_m_argument_does_not_swallow_the_next_option`.
- X2. `rust/pg-hook/tests/commit_message_parity.rs::auto_comment_char_is_the_one_git_chose_before_the_editor_ran`.
- X3. `rust/pg-hook/tests/commit_message_parity.rs::scissors_cleanup_without_an_editor_keeps_comments_like_git`.
- X4. `rust/pg-hook/tests/commit_message_parity.rs::no_message_reopens_the_editor_and_the_hook_follows_git_cutoff`.
- X5. Unit tests in `rust/pg-hook/src/commit_args.rs`: `empty_arguments_are_kept_from_cmdline` (feed a byte string with `\0\0`, through a split helper shared with `parent()`), `m_buffers_join_like_opt_parse_m`, `no_message_clears_earlier_m`, `file_and_reuse_sources_are_negated_independently` (both orders: `-C HEAD --no-file` and `--no-file -C HEAD` keep the reuse source; `-F f --no-reuse-message` and `--no-reuse-message -F f` keep the file source).
- X6. Held out: `cargo test -p pg-hook --test commit_message` unchanged and green (existing Class-Search, scissors and attribution cases).

All parity tests use real `git commit` in a temp repo (`#![cfg(unix)]`, same env isolation as `commit_message.rs`). Copy the three helpers (`git`, `repo`, `commit_with_env`) into the new file rather than editing `commit_message.rs`.

## Scope and reuse

- R1. One argument model (`commit_args.rs`) and one validator bridge (`ci_commit_gate.py` through `runpy`); no new parser, no change to `pg-policy` or `ci_commit_gate.py`.
- N1. No change to the scissors detection, merge handling, install path or the CI range check. Sources other than `-m` keep the current auto-char rule (D3).

### File budgets

Per-file caps are in the card `line_caps` and the TSV. `commit_message_parity.rs` is a new file because `commit_message.rs` is at 598 lines. `docs/rust-hooks.md` is 183 lines at 6e0289ba1 and 229 on main; its cap allows the rebased file.

Review r1 WRONG :14-:17 (citations of `commit_args.rs` / `commit_msg.rs`): rejected, they are pinned `@6e0289ba`, the base commit of this spec (lane/batch172-b133-replan-r8), where the review confirmed them; main has no `commit_args.rs`.
Review r1 B3: rejected, the accepted example already separates the comment from the trailer paragraph: `-m "x\n\n# kept\n\nWriter: codex"` (row B3 of this spec), so `Writer: codex` is its own last paragraph [tools/ci_commit_gate.py@6e0289ba:304-320].

### Implementation

- commit_args.rs. Add `fn split_cmdline(bytes: &[u8]) -> Vec<String>` used by `parent()`; change the struct per D2; in the long-option branch handle `message` (append to or clear `m_text`), `file` sets or clears `file_source` and `reuse-message` sets or clears `reuse_source`; in the short cluster `m` appends its value (attached rest of the cluster or the next argument, which may be empty), `F` sets `file_source` and `C` sets `reuse_source`. Add `pub fn auto_comment_char(&self) -> Option<u8>` implementing D3 on `m_text`.
- commit_msg.rs. In `run`, when the configured value is `auto` and `args.and_then(CommitArgs::auto_comment_char)` is `Some`, use it as `auto_char`; otherwise call the existing `auto_comment_char(raw, editor)`. Change `strips_comments` per D4.
- docs/rust-hooks.md. In the commit-msg section, state the four rules (empty `-m`, `--no-message`, scissors without an editor = whitespace, auto char from the `-m` text) and the D3 limit.
- changelog.d/COMMIT-MSG-HOOK-GIT-PARITY.md. One line: the commit-msg hook validates the message Git saves for empty `-m`, `--no-message`, scissors without an editor and an auto comment char.

## Open points for design approval

None.
