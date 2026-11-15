Pre-push now checks committed changed files with the installed size checker and
origin/main's size baseline and exemptions. A branch may change the size checker
and prune its baseline together without rechecking unchanged legacy files with
incompatible candidate data. Candidate checker/exemption changes cannot admit a
new oversized file, and growing a changed legacy file still violates its main
baseline. Each pushed ref is checked, including refs other than the checkout's
HEAD. Preflight repeats this same size check; public scrub, commit and changelog
gates retain their existing behavior. Whole-tree CI size checks are unchanged.

Rust regression: push_tool_and_baseline_change_passes_but_oversize_and_scrub_fail
failed before the fix on the removed baseline row. It also exercises dirty disk
contents, attempted candidate exemptions, baseline growth and the public scrub.

Open: the analogous language-policy checker/allowlist-directory migration needs
its own compatibility rule. This change does not execute candidate gate code.
After merge, the shared hooks checkout must follow main to install this fix;
this writer does not update that external checkout or push the branch.
