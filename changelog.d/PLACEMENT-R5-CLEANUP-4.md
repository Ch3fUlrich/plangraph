### Fixed

- None-mode workspace launches refuse a missing or non-directory `worktrees_root`
  with the existing launch verification refusal before using paths beneath it.
  None-mode never creates the root; other workspace modes are unchanged.
- Regression coverage checks missing roots, files in place of roots, and existing
  directories (D-4030, D-4103).
