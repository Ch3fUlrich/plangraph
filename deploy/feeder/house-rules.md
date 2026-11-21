Native writer rules: implement in Rust with Rust tests. No new Python, shell or
Node source files or additions to tools/new_code_allowlist.txt without an operator
D-id. Fixes inside existing Python files remain allowed until their port.
Use repository hooks as installed; never bypass hooks or change git config (D-5969).
Never push. Commit your work before the final review.
Run the relevant Rust tests, clippy with -D warnings and fmt --check, then the
repository file size, new code language, changelog/docs and commit trailer gates.
Run $HOME/.local/share/plangraph/handoffs/plan-order/bin/fast-checks.sh
with the current repository path as its argument.
Review with repository access: set REVIEW_WORKTREE=$PWD and run
$HOME/.local/share/plangraph/handoffs/plan-order/bin/agy-review.sh
with tag feed-<head8>, base origin/main, full HEAD and a prompt file stating the
goal and rules, demanding every changed file and hunk be named, no background
commands, FINDING/VERDICT and a TEST_GAMING line. Review routing must use role
review and writer_families listing the family of every Writer trailer in base..head
(your own family included). Store the review under the card's review_dir
with HEAD's eight-character SHA in its filename. Require VERDICT: ACCEPT naming
files; fix REQUEST_CHANGES in the same session and repeat fast checks/review until
ACCEPT or the round stop rule of rust/pg-spawnd/src/round_progress.rs (at most 8 rounds).
Final message: max 10 lines, include validation and any remaining limitation.
Every non-merge commit ends with all trailers in ONE final paragraph:

Plan-Item: {{item_key}}
Writer: codex/gpt-6.1-sol-medium (native-codex spawner run)
Changelog: <fragment path or skip (reason), as the check requires>
