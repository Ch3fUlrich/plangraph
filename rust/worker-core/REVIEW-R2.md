# Round 2: triage of the cross-family review (gpt-oss-120b)

Spec: rethink-setup design, sections 4.4, 4.5, 14a. C = confirmed, F = false, D = deferred.
Fix commit for every C row: `df17adf3` (tests: `worktree::option_like_base_is_refused`,
`confine::option_like_base_is_refused_not_passed_to_git`, `janitor::option_like_base_keeps_attempt_with_and_without_worktree`).

| Finding | Sev | Verdict | Evidence |
|---|---|---|---|
| Renames not reported | HIGH | F | `confine::changed_files` uses `--no-renames`: a rename is a delete plus an add, both paths listed. Test `rename_reports_both_paths`. |
| Promotion without clean-worktree check | HIGH | F | Design 4.5: "Promotion = the next leg tier ... from a clean worktree" means the promoted attempt starts on a fresh worktree from the base sha (`worktree::create`); `budget::next_action` is pure and only returns `Promote{tier}`. Context overflow returns before promotion (`budget/mod.rs` the `last.context_overflow` branch). |
| FlakyCheck quarantines immediately | HIGH | F | Design 4.5: "two identical failures with an unchanged diff -> flaky_check -> quarantine the card to the Inbox". `flaky_check` is the class after detection, and it maps to quarantine. Same action for the unflagged pair: `next_action` rule 4. |
| Dot-files bypass `src/**` | MED | F | globset `*`/`**` match dot-files. Test `dotfiles_match_globs`. |
| Ignored dirs omitted | MED | F | Ignored files cannot enter a commit; a force-added one shows in `git diff`. Test `ignored_files_are_not_changes_but_force_added_ones_are`. Flagging ignored build output would break normal runs. |
| Symlink pointing outside the repo | MED | D | Git sees the link path, which is confined like any path (a link to an in-repo forbidden dir shows the real path as changed). Writes through a link to outside the repo need a sandbox in the harness, not git diffing. |
| `../` in git paths | MED | F | git never prints `..` components in `diff --name-only` or `ls-files`. |
| Submodule changes invisible | MED | F | A dirty or advanced submodule is listed by its path. Test `dirty_submodule_is_reported`. |
| Stall false positive (pipe race) | MED | F | `stall::read_tail` drains continuously; a full pipe means the child is blocked, so no new output exists. Window is 10 min, reader lag is microseconds. |
| Double SIGKILL / group not checked | MED | F | `stall::supervise` ignores ESRCH; the last `killpg` is the intended "leave nothing behind" (design: kill the process group). |
| Orphaned grandchildren not waited on | MED | F | They are SIGKILLed with the group and reparented by the kernel; `whole_group_dies_and_pipe_closes`. |
| Branch-name injection via card id | MED | F | The id sits after `attempt/` and is the value of `-b`, never a flag. Related real bug found: the base sha was positional, see next row. |
| Unvalidated `base_for` / base sha | MED | C | `worktree add ... -f` started from the main HEAD; `diff --binary --output=<f>` wrote a file. Fixed by `git::check_rev` plus `--` in `worktree::create`. Rev-list in the janitor already errored (attempt kept), now explicit. |
| Branch deleted later after failed probe | MED | F | `janitor::sweep` does `continue` after a probe error; branch and worktree stay. Test `probe_failure_keeps_attempt`. |
| ProbeFail backoff off-by-one | MED | F | Design gives no formula ("probe_fail = backoff retry"). Streak n >= 1, so `n - 1` cannot underflow; the first retry waits the base. |
| `reroute_or_wait` double-counting | MED | F | The failed leg is only treated as limited; nothing is counted. Design: "wait only when every allowed leg is limited". |
| Usage-limit vs outage precedence | LOW | F | Both go to `reroute_or_wait`; no behaviour differs. |
| Trailing newline defeats `\b429\b` | LOW | F | `\b` matches before `\n`. Test `trailing_newline_and_mixed_patterns_still_classify`. |
| Missing tests | LOW | C | Added for rename, dot-files, ignored, submodule, newline, tail truncation, option-like revisions. |
| FNV not constant time | LOW | F | Identity hash for diff comparison, not a secret or a MAC. |
| Violation order non-deterministic | LOW | F | `changed_files` returns a `BTreeSet` (sorted). |
| `max_output` truncation too aggressive | LOW | F | Drains to exactly `max` bytes. Test `tail_keeps_exactly_the_last_max_bytes` (max 0, 1, 7). |

Counts: 2 confirmed (1 behaviour fix, 1 test gap), 19 false, 1 deferred (symlink sandbox, RT-P1-WORKER glue/harness).

Not in the review, for the glue: design 4.5 says the wait is "capped at 24 h, then park + alarm"; `budget::next_action` caps
each delay but never parks after repeated waits, and `probe_fail` backoff has no attempt limit. The glue should bound both.
